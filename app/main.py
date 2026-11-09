from datetime import date, datetime, timedelta
import calendar
from pathlib import Path
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware
from hashlib import pbkdf2_hmac
from hmac import compare_digest
import secrets
import os
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .database import init_db, query, execute, transaction

ROOT = Path(__file__).resolve().parent
init_db()
APP_ENV = os.getenv("APP_ENV", "local").strip().lower()
app = FastAPI(title="Studio Débora Ventura", version="1.4.4")
# Em produção, senha e segredo da sessão vêm exclusivamente das variáveis seguras do Render.
MASTER_PASSWORD = os.getenv("STUDIO_MASTER_PASSWORD", "1015")
if APP_ENV == "production" and len(MASTER_PASSWORD) < 12:
    raise RuntimeError("Configure STUDIO_MASTER_PASSWORD com pelo menos 12 caracteres no Render.")
MASTER_PASSWORD_HASH = pbkdf2_hmac(
    "sha256", MASTER_PASSWORD.encode("utf-8"),
    b"studio-debora-ventura-password-v1", 310_000
).hex()
SESSION_SECRET = os.getenv("SESSION_SECRET_KEY", "").strip()
if not SESSION_SECRET:
    if APP_ENV == "production":
        raise RuntimeError("Configure SESSION_SECRET_KEY nas variáveis de ambiente do Render.")
    SESSION_SECRET_FILE = ROOT.parent / ".studio_session_secret"
    if not SESSION_SECRET_FILE.exists():
        SESSION_SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    try:
        SESSION_SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    SESSION_SECRET = SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
app.add_middleware(
    SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax",
    https_only=(APP_ENV == "production")
)

@app.middleware("http")
async def require_local_login(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/health") or path.startswith("/static/"):
        return await call_next(request)
    if not request.session.get("authenticated"):
        return RedirectResponse(url=f"/login?next={path}", status_code=303)
    return await call_next(request)
# SessionMiddleware precisa envolver o middleware de autenticação para disponibilizar request.session.
app.user_middleware.insert(0, app.user_middleware.pop())
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")

def render(request, name, **context):
    return templates.TemplateResponse(request=request, name=name, context=context)

def money(value):
    return float(value or 0)

@app.get("/health")
def health():
    return {"status": "ok", "app": "Studio Débora Ventura", "version": "1.4.4"}

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error="", next=next)

@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    supplied = pbkdf2_hmac("sha256", password.encode("utf-8"), b"studio-debora-ventura-password-v1", 310_000).hex()
    if compare_digest(supplied, MASTER_PASSWORD_HASH):
        request.session.clear()
        request.session["authenticated"] = True
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(safe_next, status_code=303)
    return render(request, "login.html", error="Senha incorreta. Tente novamente.", next=next)

@app.post("/sair")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = date.today().isoformat()
    clients = query("SELECT COUNT(*) n FROM clients")[0]["n"]
    services = query("SELECT COUNT(*) n FROM services WHERE active=1")[0]["n"]
    products = query("SELECT COUNT(*) n FROM products")[0]["n"]
    appointments = query("SELECT COUNT(*) n FROM appointments WHERE substr(starts_at,1,10)=?", (today,))[0]["n"]
    financial = query("""SELECT
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Recebido' THEN amount ELSE 0 END),0) received,
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Pendente' THEN amount ELSE 0 END),0) pending
        FROM cash_movements WHERE substr(created_at,1,10)=?""", (today,))[0]
    low_stock = query("SELECT * FROM products WHERE stock_qty <= min_stock ORDER BY name LIMIT 5")
    upcoming = query("""SELECT a.*, c.name client_name, s.name service_name
        FROM appointments a JOIN clients c ON c.id=a.client_id
        JOIN services s ON s.id=a.service_id
        WHERE a.starts_at >= ? ORDER BY a.starts_at LIMIT 5""", (datetime.now().isoformat(timespec="minutes"),))
    return render(request, "dashboard.html", page_date=date.today().strftime("%d/%m/%Y"),
                  clients=clients, services=services, products=products, appointments=appointments,
                  financial=financial, low_stock=low_stock, upcoming=upcoming)

@app.get("/clientes", response_class=HTMLResponse)
def clients_page(request: Request, q: str = ""):
    rows = query("SELECT * FROM clients WHERE name LIKE ? OR phone LIKE ? ORDER BY name",
                 (f"%{q}%", f"%{q}%"))
    return render(request, "clients.html", rows=rows, q=q)

@app.post("/clientes")
def create_client(name: str = Form(...), phone: str = Form(""), email: str = Form(""), notes: str = Form("")):
    execute("INSERT INTO clients(name,phone,email,notes) VALUES(?,?,?,?)",
            (name.strip(), phone.strip(), email.strip(), notes.strip()))
    return RedirectResponse("/clientes", status_code=303)

@app.get("/clientes/{client_id}", response_class=HTMLResponse)
def client_detail(request: Request, client_id: int):
    clients = query("SELECT * FROM clients WHERE id=?", (client_id,))
    if not clients:
        raise HTTPException(404, "Cliente não encontrado")
    client = clients[0]
    sales = query("""SELECT s.*, COALESCE(STRING_AGG(si.description || ' x' || si.quantity, ', '),'') items
        FROM sales s LEFT JOIN sale_items si ON si.sale_id=s.id
        WHERE s.client_id=? GROUP BY s.id ORDER BY s.created_at DESC""", (client_id,))
    appointments = query("""SELECT a.*, sv.name service_name FROM appointments a
        JOIN services sv ON sv.id=a.service_id WHERE a.client_id=? ORDER BY a.starts_at DESC""", (client_id,))
    payments = query("""SELECT p.*, c.name client_name FROM payments p
        JOIN sales s ON s.id=p.sale_id LEFT JOIN clients c ON c.id=s.client_id
        WHERE s.client_id=? ORDER BY p.created_at DESC""", (client_id,))
    totals = query("""SELECT COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) paid,
        COALESCE(SUM(total-amount_paid),0) debt FROM sales WHERE client_id=?""", (client_id,))[0]
    return render(request, "client_detail.html", client=client, sales=sales, appointments=appointments, payments=payments, totals=totals)

@app.get("/servicos", response_class=HTMLResponse)
def services_page(request: Request):
    return render(request, "services.html", rows=query("SELECT * FROM services ORDER BY name"))

@app.post("/servicos")
def create_service(name: str = Form(...), duration_minutes: int = Form(60), price: float = Form(0), estimated_cost: float = Form(0)):
    if duration_minutes < 5 or price < 0 or estimated_cost < 0:
        raise HTTPException(400, "Confira duração e valores")
    execute("INSERT INTO services(name,duration_minutes,price,estimated_cost) VALUES(?,?,?,?)",
            (name.strip(), duration_minutes, price, estimated_cost))
    return RedirectResponse("/servicos", status_code=303)

@app.get("/produtos", response_class=HTMLResponse)
def products_page(request: Request):
    rows = query("SELECT * FROM products ORDER BY name")
    return render(request, "products.html", rows=rows)

@app.post("/produtos")
def create_product(name: str = Form(...), sku: str = Form(""), cost_price: float = Form(0),
                   sale_price: float = Form(0), stock_qty: int = Form(0), min_stock: int = Form(0)):
    if min(cost_price, sale_price, stock_qty, min_stock) < 0:
        raise HTTPException(400, "Valores não podem ser negativos")
    product_id = execute("""INSERT INTO products(name,sku,cost_price,sale_price,stock_qty,min_stock)
        VALUES(?,?,?,?,?,?)""", (name.strip(), sku.strip(), cost_price, sale_price, stock_qty, min_stock))
    if stock_qty:
        execute("INSERT INTO stock_movements(product_id,movement_type,quantity,note) VALUES(?,?,?,?)",
                (product_id, "Entrada inicial", stock_qty, "Cadastro inicial do produto"))
    return RedirectResponse("/produtos", status_code=303)

@app.post("/estoque/entrada")
def stock_entry(product_id: int = Form(...), quantity: int = Form(...), note: str = Form("")):
    if quantity <= 0:
        raise HTTPException(400, "A quantidade deve ser maior que zero")
    def work(conn):
        product = conn.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Produto não encontrado")
        conn.execute("UPDATE products SET stock_qty=stock_qty+? WHERE id=?", (quantity, product_id))
        conn.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,note) VALUES(?,?,?,?)",
                     (product_id, "Entrada", quantity, note.strip()))
    transaction(work)
    return RedirectResponse("/produtos", status_code=303)

@app.get("/agenda", response_class=HTMLResponse)
def agenda_page(request: Request, mes: str = ""):
    today = date.today()
    try:
        year, month = map(int, mes.split("-")) if mes else (today.year, today.month)
        first = date(year, month, 1)
    except Exception:
        first = today.replace(day=1); year, month = first.year, first.month
    last_day = calendar.monthrange(year, month)[1]
    prev_month = (date(year, month, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_month = (date(year + (month == 12), 1 if month == 12 else month + 1, 1)).strftime("%Y-%m")
    grid_start = first - timedelta(days=first.weekday())
    grid_end = date(year, month, last_day) + timedelta(days=(6-date(year,month,last_day).weekday()))
    rows = query("""SELECT a.*, c.name client_name,
        COALESCE((SELECT STRING_AGG(ai.description || CASE WHEN ai.quantity>1 THEN ' × '||ai.quantity ELSE '' END, ', ') FROM appointment_items ai WHERE ai.appointment_id=a.id),s.name) service_name
        FROM appointments a JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id
        WHERE substr(a.starts_at,1,10)>=? AND substr(a.starts_at,1,10)<=? ORDER BY a.starts_at""",
        (grid_start.isoformat(), grid_end.isoformat()))
    by_day = {}
    for r in rows: by_day.setdefault(r["starts_at"][:10], []).append(dict(r))
    weeks=[]; cursor=grid_start
    while cursor <= grid_end:
        week=[]
        for _ in range(7):
            key=cursor.isoformat(); week.append({"date":key,"day":cursor.day,"current":cursor.month==month,"today":cursor==today,"items":by_day.get(key,[])})
            cursor += timedelta(days=1)
        weeks.append(week)
    clients = query("SELECT id,name FROM clients ORDER BY name")
    services = query("SELECT id,name,price,duration_minutes FROM services WHERE active=1 ORDER BY name")
    recent = query("""SELECT a.*, c.name client_name,
        COALESCE((SELECT STRING_AGG(ai.description || CASE WHEN ai.quantity>1 THEN ' × '||ai.quantity ELSE '' END, ', ') FROM appointment_items ai WHERE ai.appointment_id=a.id),s.name) service_name
        FROM appointments a JOIN clients c ON c.id=a.client_id JOIN services s ON s.id=a.service_id
        ORDER BY a.starts_at DESC LIMIT 100""")
    return render(request,"agenda.html",clients=clients,services=services,rows=recent,weeks=weeks,
        month_label=['janeiro','fevereiro','março','abril','maio','junho','julho','agosto','setembro','outubro','novembro','dezembro'][month-1] + f' de {year}',prev_month=prev_month,next_month=next_month,
        selected_month=f"{year:04d}-{month:02d}",page_date=today.strftime("%d/%m/%Y"))

@app.get("/agenda/{appointment_id}/editar", response_class=HTMLResponse)
def edit_appointment_page(request: Request, appointment_id: int):
    rows=query("SELECT * FROM appointments WHERE id=?",(appointment_id,))
    if not rows: raise HTTPException(404,"Agendamento não encontrado")
    items=query("SELECT * FROM appointment_items WHERE appointment_id=? ORDER BY id",(appointment_id,))
    return render(request,"appointment_edit.html",appt=rows[0],items=items,clients=query("SELECT id,name FROM clients ORDER BY name"),services=query("SELECT id,name,price,duration_minutes FROM services WHERE active=1 ORDER BY name"))

@app.post("/agenda/{appointment_id}/editar")
def edit_appointment(appointment_id:int, client_id:int=Form(...), service_ids:list[int]=Form(...), quantities:list[int]=Form(...), appointment_date:str=Form(...), appointment_time:str=Form(...), notes:str=Form(""), due_date:str=Form("")):
    old=query("SELECT * FROM appointments WHERE id=?",(appointment_id,))
    if not old: raise HTTPException(404,"Agendamento não encontrado")
    if not service_ids or len(service_ids)!=len(quantities) or any(q<1 for q in quantities): raise HTTPException(400,"Selecione os serviços e quantidades")
    services=[]; total=0.0
    for sid,qty in zip(service_ids,quantities):
        found=query("SELECT * FROM services WHERE id=? AND active=1",(sid,))
        if not found: raise HTTPException(404,"Serviço não encontrado")
        sv=found[0]; line=round(float(sv['price'])*qty,2); total+=line; services.append((sid,sv['name'],qty,float(sv['price']),line))
    paid=float(old[0]['amount_paid'])
    if total < paid: raise HTTPException(400,"O total não pode ficar abaixo do valor já recebido")
    try: parsed=datetime.fromisoformat(f"{appointment_date}T{appointment_time}")
    except ValueError: raise HTTPException(400,"Confira a data e o horário do atendimento")
    status="Pago" if total-paid<=.001 else ("Parcial" if paid else "Faturado")
    first_service=services[0]
    def work(conn):
        conn.execute("UPDATE appointments SET client_id=?,service_id=?,starts_at=?,notes=?,total_amount=?,billing_status=?,due_date=? WHERE id=?",(client_id,first_service[0],parsed.isoformat(timespec='minutes'),notes.strip(),round(total,2),status,due_date,appointment_id))
        conn.execute("DELETE FROM appointment_items WHERE appointment_id=?",(appointment_id,))
        for sid,name,qty,price,line in services: conn.execute("INSERT INTO appointment_items(appointment_id,service_id,description,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?)",(appointment_id,sid,name,qty,price,line))
        conn.execute("UPDATE cash_movements SET amount=?,description=?,due_date=? WHERE appointment_id=? AND status='Pendente'",(max(0,round(total-paid,2)),f"Saldo atualizado — {', '.join(x[1] for x in services)}",due_date,appointment_id))
        conn.execute("DELETE FROM cash_movements WHERE appointment_id=? AND status='Pendente' AND amount<=.001",(appointment_id,))
        if total-paid>.001 and not conn.execute("SELECT id FROM cash_movements WHERE appointment_id=? AND status='Pendente'",(appointment_id,)).fetchone():
            conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,due_date,appointment_id) VALUES('servicos','Entrada',?,?,'Faturado','Pendente',?,?)",(round(total-paid,2),f"Saldo atualizado — atendimento #{appointment_id}",due_date,appointment_id))
    transaction(work); return RedirectResponse('/agenda',status_code=303)

@app.post("/agenda")
def create_appointment(client_id:int=Form(...), service_ids:list[int]=Form(...), quantities:list[int]=Form(...), appointment_date:str=Form(...), appointment_time:str=Form(...), notes:str=Form(""), payment_method:str=Form("Pendente"), amount_paid:float=Form(0), due_date:str=Form("")):
    try: parsed=datetime.fromisoformat(f"{appointment_date}T{appointment_time}")
    except ValueError: raise HTTPException(400,"Confira a data e o horário do atendimento")
    if len(service_ids)!=len(quantities) or not service_ids or any(q<1 for q in quantities): raise HTTPException(400,"Adicione ao menos um serviço e confira as quantidades")
    if not query("SELECT id FROM clients WHERE id=?",(client_id,)): raise HTTPException(400,"Cliente não encontrada")
    lines=[]; total=0.0
    for sid,qty in zip(service_ids,quantities):
        found=query("SELECT * FROM services WHERE id=? AND active=1",(sid,))
        if not found: raise HTTPException(400,"Selecione serviços válidos")
        sv=found[0]; line=round(float(sv['price'])*qty,2); total+=line; lines.append((sid,sv['name'],qty,float(sv['price']),line))
    total=round(total,2)
    if amount_paid<0 or amount_paid>total: raise HTTPException(400,"Confira o valor recebido")
    if amount_paid<total and not due_date: due_date=parsed.date().isoformat()
    status="Pago" if amount_paid>=total-.001 else ("Parcial" if amount_paid>0 else "Faturado")
    def work(conn):
        cur=conn.execute("INSERT INTO appointments(client_id,service_id,starts_at,notes,total_amount,amount_paid,payment_method,billing_status,due_date) VALUES(?,?,?,?,?,?,?,?,?)",(client_id,lines[0][0],parsed.isoformat(timespec='minutes'),notes.strip(),total,amount_paid,payment_method,status,due_date)); aid=cur.lastrowid
        for sid,name,qty,price,line in lines: conn.execute("INSERT INTO appointment_items(appointment_id,service_id,description,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?)",(aid,sid,name,qty,price,line))
        label=f"Atendimento #{aid} — " + ', '.join(x[1] for x in lines)
        if amount_paid>0: conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,appointment_id) VALUES('servicos','Entrada',?,?,?,'Recebido',?)",(amount_paid,label,payment_method,aid))
        balance=round(total-amount_paid,2)
        if balance>0: conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,due_date,appointment_id) VALUES('servicos','Entrada',?,?,'Faturado','Pendente',?,?)",(balance,f"Conta a receber — {label}",due_date,aid))
        return aid
    aid=transaction(work); return RedirectResponse(f"/agenda?registrado={aid}",status_code=303)

@app.post("/agenda/{appointment_id}/receber")
def receive_appointment_payment(appointment_id: int, amount: float = Form(...),
                                payment_method: str = Form("Pix"), note: str = Form("")):
    if amount <= 0:
        raise HTTPException(400,"O valor deve ser maior que zero")
    def work(conn):
        appt=conn.execute("SELECT * FROM appointments WHERE id=?",(appointment_id,)).fetchone()
        if not appt:
            raise HTTPException(404,"Agendamento não encontrado")
        balance=round(float(appt["total_amount"])-float(appt["amount_paid"]),2)
        if amount > balance + 0.001:
            raise HTTPException(400,"O recebimento supera o saldo pendente")
        new_paid=round(float(appt["amount_paid"])+amount,2)
        status="Pago" if new_paid >= float(appt["total_amount"])-0.001 else "Parcial"
        conn.execute("UPDATE appointments SET amount_paid=?,payment_method=?,billing_status=? WHERE id=?",
                     (new_paid,payment_method,status,appointment_id))
        linked_sale = conn.execute("SELECT id FROM sales WHERE appointment_id=?",(appointment_id,)).fetchone()
        if linked_sale:
            sale_id = linked_sale["id"]
            conn.execute("UPDATE sales SET amount_paid=?,status=?,payment_method=? WHERE id=?",
                         (new_paid,status,payment_method,sale_id))
            conn.execute("INSERT INTO payments(sale_id,amount,payment_method,note) VALUES(?,?,?,?)",
                         (sale_id,amount,payment_method,note.strip() or "Recebimento pelo atendimento"))
            remaining_payment=amount
            installments=conn.execute("SELECT * FROM sale_installments WHERE sale_id=? ORDER BY installment_no",(sale_id,)).fetchall()
            for inst in installments:
                due=max(0.0,float(inst["amount"])-float(inst["amount_paid"]))
                applied=min(due,remaining_payment)
                if applied>0:
                    paid_now=round(float(inst["amount_paid"])+applied,2)
                    state="Pago" if paid_now>=float(inst["amount"])-.001 else "Parcial"
                    conn.execute("UPDATE sale_installments SET amount_paid=?,status=?,paid_at=? WHERE id=?",
                                 (paid_now,state,datetime.now().isoformat(timespec="minutes"),inst["id"]))
                    remaining_payment=round(remaining_payment-applied,2)
                if remaining_payment<=.001: break
            sale_type=conn.execute("SELECT item_type FROM sale_items WHERE sale_id=? LIMIT 1",(sale_id,)).fetchone()
            cash_type="servicos" if sale_type and sale_type["item_type"]=="servico" else "produtos"
            conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,sale_id) VALUES(?,'Entrada',?,?,?,'Recebido',?)",
                         (cash_type,amount,note.strip() or f"Recebimento do atendimento #{appointment_id}",payment_method,sale_id))
            remaining=round(float(appt["total_amount"])-new_paid,2)
            conn.execute("UPDATE cash_movements SET amount=? WHERE sale_id=? AND status='Pendente'",(max(0,remaining),sale_id))
            conn.execute("DELETE FROM cash_movements WHERE sale_id=? AND status='Pendente' AND amount<=.001",(sale_id,))
        else:
            conn.execute("""INSERT INTO cash_movements
                (cash_type,direction,amount,description,payment_method,status,appointment_id)
                VALUES('servicos','Entrada',?,?,?,'Recebido',?)""",
                (amount,note.strip() or "Recebimento de atendimento",payment_method,appointment_id))
            # Reduce the pending receivable to the remaining balance.
            conn.execute("""UPDATE cash_movements SET amount=?,status='Pendente'
                WHERE appointment_id=? AND status='Pendente'""",(max(0,round(float(appt["total_amount"])-new_paid,2)),appointment_id))
            conn.execute("DELETE FROM cash_movements WHERE appointment_id=? AND status='Pendente' AND amount<=0.001",(appointment_id,))
    transaction(work)
    return RedirectResponse("/agenda",status_code=303)

@app.get("/vendas", response_class=HTMLResponse)
def sales_page(request: Request):
    clients = query("SELECT id,name FROM clients ORDER BY name")
    products = query("SELECT id,name,sale_price,stock_qty FROM products ORDER BY name")
    rows = query("""SELECT s.*, c.name client_name, a.starts_at appointment_starts_at,
        COALESCE(STRING_AGG(si.description || ' × ' || si.quantity, ', '),'') items
        FROM sales s LEFT JOIN clients c ON c.id=s.client_id
        LEFT JOIN appointments a ON a.id=s.appointment_id
        LEFT JOIN sale_items si ON si.sale_id=s.id
        WHERE EXISTS (SELECT 1 FROM sale_items sx WHERE sx.sale_id=s.id AND sx.item_type='produto')
        GROUP BY s.id, c.name, a.starts_at ORDER BY s.created_at DESC LIMIT 100""")
    enriched=[]
    for row in rows:
        item=dict(row); item["payments"]=query("SELECT * FROM payments WHERE sale_id=? ORDER BY created_at DESC",(row["id"],)); enriched.append(item)
    return render(request, "sales.html", clients=clients, products=products, rows=enriched)

@app.post("/vendas")
def create_sale(client_id: str = Form(""), item_type: str = Form(...),
                item_id: list[int] = Form(...), quantity: list[int] = Form(...),
                payment_method: str = Form("Pendente"), amount_paid: float = Form(0),
                notes: str = Form(""), due_date: str = Form(""),
                discount_type: str = Form("valor"), discount: float = Form(0),
                installments: int = Form(1), starts_at: str = Form("")):
    if item_type != "produto" or len(item_id) != len(quantity) or not item_id:
        raise HTTPException(400, "A tela de vendas é exclusiva para produtos. Agende serviços pela Agenda.")
    if any(q < 1 for q in quantity) or amount_paid < 0 or discount < 0 or installments < 1 or installments > 36:
        raise HTTPException(400, "Confira quantidades, desconto, pagamento e parcelas")
    client_value = int(client_id) if client_id.strip() else None
    if client_value and not query("SELECT id FROM clients WHERE id=?", (client_value,)):
        raise HTTPException(400, "Cliente não encontrado")
    lines=[]; subtotal=0.0
    for iid, qty in zip(item_id, quantity):
        found = query("SELECT * FROM products WHERE id=?", (iid,))
        if not found: raise HTTPException(404, "Produto ou serviço não encontrado")
        item=found[0]
        if item_type == "produto" and item["stock_qty"] < qty:
            raise HTTPException(400, f"Estoque insuficiente: {item['name']}")
        unit_price=money(item["sale_price"])
        unit_cost=money(item["cost_price"])
        line=round(unit_price*qty,2); subtotal+=line
        lines.append((iid,qty,item["name"],unit_price,unit_cost,line))
    subtotal=round(subtotal,2)
    discount_value=round(min(subtotal, subtotal*discount/100 if discount_type=="percentual" else discount),2)
    total=round(subtotal-discount_value,2)
    if amount_paid > total + .001: raise HTTPException(400, "Valor recebido não pode superar o total com desconto")
    status="Pago" if amount_paid >= total-.001 else ("Parcial" if amount_paid>0 else "Pendente")
    def work(conn):
        cur=conn.execute("""INSERT INTO sales(client_id,total,payment_method,amount_paid,status,notes,discount,discount_type)
            VALUES(?,?,?,?,?,?,?,?)""",(client_value,total,payment_method,amount_paid,status,notes.strip(),discount_value,discount_type))
        sale_id=cur.lastrowid; cash_type="produtos"
        names=[]
        for iid,qty,name,price,cost,line in lines:
            names.append(f"{name} x{qty}")
            conn.execute("""INSERT INTO sale_items(sale_id,item_type,item_id,description,quantity,unit_price,unit_cost,line_total)
                VALUES(?,?,?,?,?,?,?,?)""",(sale_id,item_type,iid,name,qty,price,cost,line))
            if item_type=="produto":
                conn.execute("UPDATE products SET stock_qty=stock_qty-? WHERE id=?",(qty,iid))
                conn.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,note,sale_id) VALUES(?,?,?,?,?)",(iid,"Venda",-qty,f"Venda #{sale_id}",sale_id))
        label=f"Venda #{sale_id} — " + ", ".join(names)
        if amount_paid>0:
            conn.execute("INSERT INTO payments(sale_id,amount,payment_method,note) VALUES(?,?,?,?)",(sale_id,amount_paid,payment_method,"Pagamento inicial"))
            conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,sale_id) VALUES(?,'Entrada',?,?,?,'Recebido',?)",(cash_type,amount_paid,label,payment_method,sale_id))
        balance=round(total-amount_paid,2)
        if balance>0:
            conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,due_date,sale_id) VALUES(?,'Entrada',?,?,'Faturado','Pendente',?,?)",(cash_type,balance,f"Conta a receber — {label}",due_date,sale_id))
            base_date=date.fromisoformat(due_date) if due_date else date.today()+timedelta(days=30)
            cents=int(round(balance*100)); each=cents//installments; remainder=cents%installments
            for n in range(1,installments+1):
                month_index=base_date.month-1+(n-1)
                year=base_date.year+month_index//12; month=month_index%12+1
                due=date(year,month,min(base_date.day,calendar.monthrange(year,month)[1]))
                value=(each+(1 if n<=remainder else 0))/100
                conn.execute("INSERT INTO sale_installments(sale_id,installment_no,amount,due_date) VALUES(?,?,?,?)",(sale_id,n,value,due.isoformat()))
        return sale_id
    sale_id=transaction(work)
    return RedirectResponse(f"/vendas?registrada={sale_id}", status_code=303)

@app.get("/vendas/{sale_id}/editar", response_class=HTMLResponse)
def edit_sale_page(request: Request, sale_id: int):
    sales=query("SELECT * FROM sales WHERE id=?",(sale_id,))
    if not sales: raise HTTPException(404,"Venda não encontrada")
    sale=sales[0]; items=query("SELECT * FROM sale_items WHERE sale_id=?",(sale_id,))
    item_subtotal=sum(float(i["line_total"]) for i in items)
    discount_input=(float(sale["discount"])/item_subtotal*100 if sale["discount_type"]=="percentual" and item_subtotal else float(sale["discount"]))
    item_type=items[0]["item_type"] if items else "produto"
    catalog=query("SELECT id,name,sale_price price,stock_qty FROM products ORDER BY name") if item_type=="produto" else query("SELECT id,name,price,0 stock_qty FROM services WHERE active=1 ORDER BY name")
    return render(request,"sale_edit.html",sale=sale,items=items,catalog=catalog,clients=query("SELECT id,name FROM clients ORDER BY name"),item_type=item_type,discount_input=discount_input,payments=query("SELECT * FROM payments WHERE sale_id=? ORDER BY created_at DESC",(sale_id,)))

@app.post("/vendas/{sale_id}/editar")
def edit_sale(sale_id:int, client_id:str=Form(""), item_id:list[int]=Form(...), quantity:list[int]=Form(...), discount:float=Form(0), discount_type:str=Form("valor"), notes:str=Form("")):
    old=query("SELECT * FROM sales WHERE id=?",(sale_id,))
    old_items=query("SELECT * FROM sale_items WHERE sale_id=?",(sale_id,))
    if not old or not old_items: raise HTTPException(404,"Venda não encontrada")
    if len(item_id)!=len(quantity) or not item_id or any(q<1 for q in quantity) or discount<0: raise HTTPException(400,"Confira os itens e quantidades")
    item_type=old_items[0]["item_type"]
    if any(x["item_type"]!=item_type for x in old_items): raise HTTPException(400,"Esta operação contém tipos diferentes; mantenha o tipo original")
    lines=[]; subtotal=0
    for iid,qty in zip(item_id,quantity):
        table="products" if item_type=="produto" else "services"
        found=query(f"SELECT * FROM {table} WHERE id=?",(iid,))
        if not found: raise HTTPException(404,"Item não encontrado")
        x=found[0]; price=float(x["sale_price"] if item_type=="produto" else x["price"]); cost=float(x["cost_price"] if item_type=="produto" else x["estimated_cost"])
        if item_type=="produto":
            prior=sum(int(z["quantity"]) for z in old_items if z["item_id"]==iid)
            if int(x["stock_qty"])+prior<qty: raise HTTPException(400,f"Estoque insuficiente: {x['name']}")
        line=round(price*qty,2); subtotal+=line; lines.append((iid,qty,x["name"],price,cost,line))
    subtotal=round(subtotal,2); disc=min(subtotal,subtotal*discount/100 if discount_type=="percentual" else discount); total=round(subtotal-disc,2); paid=float(old[0]["amount_paid"])
    if total<paid: raise HTTPException(400,"O total não pode ficar abaixo do valor já recebido. Ajuste os itens ou registre um estorno separadamente.")
    client=int(client_id) if client_id.strip() else None
    def work(conn):
        if item_type=="produto":
            for z in old_items: conn.execute("UPDATE products SET stock_qty=stock_qty+? WHERE id=?",(z["quantity"],z["item_id"]))
        conn.execute("DELETE FROM sale_items WHERE sale_id=?",(sale_id,))
        for iid,qty,name,price,cost,line in lines:
            conn.execute("INSERT INTO sale_items(sale_id,item_type,item_id,description,quantity,unit_price,unit_cost,line_total) VALUES(?,?,?,?,?,?,?,?)",(sale_id,item_type,iid,name,qty,price,cost,line))
            if item_type=="produto":
                conn.execute("UPDATE products SET stock_qty=stock_qty-? WHERE id=?",(qty,iid))
                conn.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,note,sale_id) VALUES(?,?,?,?,?)",(iid,"Ajuste de venda",-qty,f"Edição da venda #{sale_id}",sale_id))
        status="Pago" if total-paid<=.001 else ("Parcial" if paid else "Pendente")
        conn.execute("UPDATE sales SET client_id=?,total=?,discount=?,discount_type=?,notes=?,status=? WHERE id=?",(client,total,disc,discount_type,notes.strip(),status,sale_id))
        linked = conn.execute("SELECT appointment_id FROM sales WHERE id=?",(sale_id,)).fetchone()
        if linked and linked["appointment_id"]:
            aid = linked["appointment_id"]
            conn.execute("UPDATE appointments SET client_id=?,service_id=?,notes=?,total_amount=?,amount_paid=?,billing_status=? WHERE id=?",
                (client,lines[0][0],notes.strip(),total,paid,status,aid))
            conn.execute("DELETE FROM appointment_items WHERE appointment_id=?",(aid,))
            for iid,qty,name,price,cost,line in lines:
                conn.execute("INSERT INTO appointment_items(appointment_id,service_id,description,quantity,unit_price,line_total) VALUES(?,?,?,?,?,?)",
                    (aid,iid,name,qty,price,line))
        conn.execute("UPDATE cash_movements SET amount=?,description=? WHERE sale_id=? AND status='Pendente'",(max(0,total-paid),f"Saldo atualizado — venda #{sale_id}",sale_id))
        conn.execute("DELETE FROM cash_movements WHERE sale_id=? AND status='Pendente' AND amount<=.001",(sale_id,))
        if total-paid>.001 and not conn.execute("SELECT id FROM cash_movements WHERE sale_id=? AND status='Pendente'",(sale_id,)).fetchone():
            conn.execute("INSERT INTO cash_movements(cash_type,direction,amount,description,payment_method,status,sale_id) VALUES(?,'Entrada',?,?,'Faturado','Pendente',?)",("produtos" if item_type=="produto" else "servicos",total-paid,f"Saldo atualizado — venda #{sale_id}",sale_id))
    transaction(work)
    return RedirectResponse("/vendas",status_code=303)


# Documentos de atendimento: orçamento e recibo, com papelaria clara e identidade dourada.
def _studio_pdf_header(pdf, w, h, title, reference):
    gold=colors.HexColor('#B58B3B'); ink=colors.HexColor('#24211B'); muted=colors.HexColor('#716B60')
    logo=ROOT/'static'/'img'/'logo-debora-ventura.png'
    pdf.setFillColor(colors.white); pdf.rect(0,0,w,h,fill=1,stroke=0)
    pdf.setStrokeColor(gold); pdf.setLineWidth(1.25); pdf.line(42,h-126,w-42,h-126)
    if logo.exists(): pdf.drawImage(str(logo),w-122,h-112,width=76,height=76,preserveAspectRatio=True,mask='auto',anchor='c')
    pdf.setFillColor(ink); pdf.setFont('Helvetica-Bold',20); pdf.drawString(46,h-58,'DÉBORA VENTURA')
    pdf.setFillColor(gold); pdf.setFont('Helvetica',9); pdf.drawString(47,h-76,'STUDIO BEAUTY')
    pdf.setFillColor(muted); pdf.setFont('Helvetica',8); pdf.drawString(47,h-94,'BELEZA · CUIDADO · EXPERIÊNCIA')
    pdf.setFillColor(ink); pdf.setFont('Helvetica-Bold',13); pdf.drawString(46,h-157,title.upper())
    pdf.setFillColor(gold); pdf.setFont('Helvetica',8); pdf.drawRightString(w-46,h-157,reference)
    return h-184

def _pdf_footer(pdf,w):
    pdf.setStrokeColor(colors.HexColor('#E5D9C0')); pdf.line(42,55,w-42,55)
    pdf.setFillColor(colors.HexColor('#827A6C')); pdf.setFont('Helvetica-Oblique',8)
    pdf.drawCentredString(w/2,39,'Studio Débora Ventura · Documento de controle e atendimento')

@app.get('/agenda/{appointment_id}/orcamento')
def appointment_quote(appointment_id:int):
    rows=query("SELECT a.*,c.name client_name,c.phone,c.email FROM appointments a JOIN clients c ON c.id=a.client_id WHERE a.id=?",(appointment_id,))
    if not rows: raise HTTPException(404,'Atendimento não encontrado')
    a=rows[0]; items=query('SELECT * FROM appointment_items WHERE appointment_id=? ORDER BY id',(appointment_id,))
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=A4); w,h=A4; y=_studio_pdf_header(pdf,w,h,'Orçamento de serviços',f'ORÇAMENTO Nº {appointment_id:06d}')
    pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',9); pdf.drawString(46,y,'CLIENTE'); pdf.setFont('Helvetica',10); pdf.drawString(46,y-17,a['client_name'])
    pdf.setFont('Helvetica',9); pdf.setFillColor(colors.HexColor('#716B60')); pdf.drawString(46,y-34,f"Telefone: {a['phone'] or 'Não informado'}")
    dt=datetime.fromisoformat(a['starts_at']); pdf.drawString(46,y-49,f"Data prevista: {dt.strftime('%d/%m/%Y às %H:%M')}"); y-=82
    pdf.setFillColor(colors.HexColor('#B58B3B')); pdf.setFont('Helvetica-Bold',8); pdf.drawString(48,y,'SERVIÇO'); pdf.drawRightString(w-245,y,'QTD.'); pdf.drawRightString(w-135,y,'VALOR UNIT.'); pdf.drawRightString(w-48,y,'TOTAL'); y-=12
    pdf.setStrokeColor(colors.HexColor('#E5D9C0')); pdf.line(46,y,w-46,y); y-=20
    subtotal=0
    for it in items:
        pdf.setFillColor(colors.HexColor('#29251E')); pdf.setFont('Helvetica',9); pdf.drawString(48,y,it['description'][:55]); pdf.drawRightString(w-245,y,str(it['quantity'])); pdf.drawRightString(w-135,y,f"R$ {it['unit_price']:.2f}"); pdf.drawRightString(w-48,y,f"R$ {it['line_total']:.2f}")
        subtotal+=float(it['line_total']); y-=35
        if y<120: _pdf_footer(pdf,w); pdf.showPage(); y=_studio_pdf_header(pdf,w,h,'Orçamento de serviços',f'ORÇAMENTO Nº {appointment_id:06d}')
    pdf.setStrokeColor(colors.HexColor('#D9C69F')); pdf.line(w-245,y,w-46,y); y-=24
    pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',11); pdf.drawRightString(w-48,y,f'TOTAL DO ORÇAMENTO  R$ {subtotal:.2f}')
    y-=38; pdf.setFillColor(colors.HexColor('#716B60')); pdf.setFont('Helvetica',9); pdf.drawString(48,y,'Este orçamento está sujeito à confirmação dos serviços e horários pelo Studio.')
    if a['notes']: y-=20; pdf.drawString(48,y,'Observações: '+a['notes'][:100])
    _pdf_footer(pdf,w); pdf.save(); buf.seek(0)
    return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename=orcamento_atendimento_{appointment_id:06d}.pdf'})

@app.get('/agenda/{appointment_id}/recibo')
def appointment_receipt(appointment_id:int):
    rows=query("SELECT a.*,c.name client_name,c.phone FROM appointments a JOIN clients c ON c.id=a.client_id WHERE a.id=?",(appointment_id,))
    if not rows: raise HTTPException(404,'Atendimento não encontrado')
    a=rows[0]; items=query('SELECT * FROM appointment_items WHERE appointment_id=? ORDER BY id',(appointment_id,))
    receipts=query("SELECT * FROM cash_movements WHERE appointment_id=? AND status='Recebido' AND direction='Entrada' ORDER BY created_at",(appointment_id,))
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=A4); w,h=A4; y=_studio_pdf_header(pdf,w,h,'Recibo de atendimento',f'RECIBO Nº {appointment_id:06d}')
    pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',9); pdf.drawString(46,y,'RECEBEMOS DE'); pdf.setFont('Helvetica',11); pdf.drawString(46,y-19,a['client_name']); y-=55
    pdf.setFont('Helvetica',9); pdf.setFillColor(colors.HexColor('#716B60')); pdf.drawString(46,y,'A importância referente aos serviços abaixo discriminados:'); y-=28
    for it in items:
        pdf.setFillColor(colors.HexColor('#29251E')); pdf.setFont('Helvetica',9); pdf.drawString(48,y,f"{it['description']} · {it['quantity']} x R$ {it['unit_price']:.2f}"); pdf.drawRightString(w-48,y,f"R$ {it['line_total']:.2f}"); y-=19
    y-=12; pdf.setStrokeColor(colors.HexColor('#D9C69F')); pdf.line(w-245,y,w-46,y); y-=24
    pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',11); pdf.drawRightString(w-48,y,f"TOTAL: R$ {a['total_amount']:.2f}"); y-=22
    pdf.setFont('Helvetica',9); pdf.drawRightString(w-48,y,f"Total recebido: R$ {a['amount_paid']:.2f}"); y-=17; pdf.drawRightString(w-48,y,f"Saldo pendente: R$ {max(0,float(a['total_amount'])-float(a['amount_paid'])):.2f}")
    y-=42; pdf.setFillColor(colors.HexColor('#716B60')); pdf.setFont('Helvetica-Bold',9); pdf.drawString(48,y,'PAGAMENTOS REGISTRADOS'); y-=17
    for r in receipts:
        pdf.setFont('Helvetica',8); pdf.drawString(48,y,f"{r['created_at'][:10]} · {r['payment_method']} · R$ {r['amount']:.2f}"); y-=14
    y-=38; pdf.setStrokeColor(colors.HexColor('#B58B3B')); pdf.line(90,y,270,y); pdf.line(330,y,510,y); y-=13
    pdf.setFont('Helvetica',8); pdf.setFillColor(colors.HexColor('#716B60')); pdf.drawCentredString(180,y,'Assinatura / Studio'); pdf.drawCentredString(420,y,'Assinatura da cliente')
    _pdf_footer(pdf,w); pdf.save(); buf.seek(0)
    return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename=recibo_atendimento_{appointment_id:06d}.pdf'})


@app.get('/vendas/{sale_id}/orcamento')
def sale_quote(sale_id:int):
    rows=query("SELECT s.*,c.name client_name,c.phone FROM sales s LEFT JOIN clients c ON c.id=s.client_id WHERE s.id=?",(sale_id,))
    if not rows: raise HTTPException(404,'Venda não encontrada')
    sale=rows[0]; items=query('SELECT * FROM sale_items WHERE sale_id=? ORDER BY id',(sale_id,))
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=A4); w,h=A4; y=_studio_pdf_header(pdf,w,h,'Orçamento de produtos e serviços',f'ORÇAMENTO Nº {sale_id:06d}')
    pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',9); pdf.drawString(46,y,'CLIENTE'); pdf.setFont('Helvetica',10); pdf.drawString(46,y-17,sale['client_name'] or 'Consumidor não identificado'); pdf.setFont('Helvetica',9); pdf.setFillColor(colors.HexColor('#716B60')); pdf.drawString(46,y-34,f"Telefone: {sale['phone'] or 'Não informado'}"); y-=65
    pdf.setFillColor(colors.HexColor('#B58B3B')); pdf.setFont('Helvetica-Bold',8); pdf.drawString(48,y,'DESCRIÇÃO'); pdf.drawRightString(w-245,y,'QTD.'); pdf.drawRightString(w-135,y,'UNITÁRIO'); pdf.drawRightString(w-48,y,'TOTAL'); y-=12; pdf.setStrokeColor(colors.HexColor('#E5D9C0')); pdf.line(46,y,w-46,y); y-=20
    subtotal=0
    for it in items:
        pdf.setFillColor(colors.HexColor('#29251E')); pdf.setFont('Helvetica',9); pdf.drawString(48,y,it['description'][:55]); pdf.drawRightString(w-245,y,str(it['quantity'])); pdf.drawRightString(w-135,y,f"R$ {it['unit_price']:.2f}"); pdf.drawRightString(w-48,y,f"R$ {it['line_total']:.2f}"); subtotal+=float(it['line_total']); y-=21
    y-=8; pdf.setStrokeColor(colors.HexColor('#D9C69F')); pdf.line(w-245,y,w-46,y); y-=22; pdf.setFillColor(colors.HexColor('#716B60')); pdf.setFont('Helvetica',9); pdf.drawRightString(w-48,y,f'Subtotal: R$ {subtotal:.2f}'); y-=17; pdf.drawRightString(w-48,y,f"Desconto: R$ {sale['discount']:.2f}"); y-=22; pdf.setFillColor(colors.HexColor('#24211B')); pdf.setFont('Helvetica-Bold',12); pdf.drawRightString(w-48,y,f"TOTAL DO ORÇAMENTO: R$ {sale['total']:.2f}")
    if sale['notes']: y-=30; pdf.setFillColor(colors.HexColor('#716B60')); pdf.setFont('Helvetica',9); pdf.drawString(48,y,'Observações: '+sale['notes'][:100])
    _pdf_footer(pdf,w); pdf.save(); buf.seek(0); return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename=orcamento_{sale_id:06d}.pdf'})

@app.get("/vendas/{sale_id}/recibo")
def sale_receipt(sale_id: int):
    sale=query("SELECT s.*,c.name client_name,c.phone,c.email FROM sales s LEFT JOIN clients c ON c.id=s.client_id WHERE s.id=?",(sale_id,))
    if not sale: raise HTTPException(404,"Venda não encontrada")
    s=sale[0]; items=query("SELECT * FROM sale_items WHERE sale_id=?",(sale_id,)); payments=query("SELECT * FROM payments WHERE sale_id=? ORDER BY created_at",(sale_id,))
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=A4); w,h=A4; y=h-55
    pdf.setFillColor(colors.white); pdf.rect(0,h-135,w,135,fill=1,stroke=0); pdf.setStrokeColor(colors.HexColor('#b58b3b')); pdf.setLineWidth(1.4); pdf.line(42,h-139,w-42,h-139)
    logo=ROOT / 'static' / 'img' / 'logo-debora-ventura.png'
    if logo.exists(): pdf.drawImage(str(logo),w-145,h-124,width=105,height=105,preserveAspectRatio=True,mask='auto',anchor='c')
    pdf.setFillColor(colors.HexColor('#211f1a')); pdf.setFont('Helvetica-Bold',19); pdf.drawString(48,y,'DÉBORA VENTURA')
    pdf.setFillColor(colors.HexColor('#9a762f')); pdf.setFont('Helvetica',9); pdf.drawString(48,y-17,'STUDIO BEAUTY  |  RECIBO DE VENDA / SERVIÇO')
    y-=55; pdf.setFont('Helvetica-Bold',12); pdf.drawString(48,y,f"Recibo nº {sale_id:06d}"); y-=25
    pdf.setFont('Helvetica',10); pdf.drawString(48,y,f"Cliente: {s['client_name'] or 'Consumidor não identificado'}"); y-=17
    pdf.drawString(48,y,f"Data: {s['created_at'][:16]}   |   Situação: {s['status']}"); y-=30
    pdf.setFont('Helvetica-Bold',9); pdf.drawString(48,y,'ITEM'); pdf.drawRightString(w-48,y,'TOTAL'); y-=12
    pdf.line(48,y,w-48,y); y-=18; pdf.setFont('Helvetica',9)
    for it in items:
        pdf.drawString(48,y,f"{it['description']}  ({it['quantity']} x R$ {it['unit_price']:.2f})"); pdf.drawRightString(w-48,y,f"R$ {it['line_total']:.2f}"); y-=18
        if y<180: pdf.showPage(); y=h-55
    y-=8; pdf.line(48,y,w-48,y); y-=22
    pdf.drawRightString(w-48,y,f"Subtotal: R$ {sum(i['line_total'] for i in items):.2f}"); y-=18
    pdf.drawRightString(w-48,y,f"Desconto: R$ {s['discount']:.2f}"); y-=20
    pdf.setFont('Helvetica-Bold',12); pdf.drawRightString(w-48,y,f"TOTAL: R$ {s['total']:.2f}"); y-=22
    pdf.setFont('Helvetica',10); pdf.drawString(48,y,f"Recebido: R$ {s['amount_paid']:.2f}   |   Saldo: R$ {max(0,s['total']-s['amount_paid']):.2f}"); y-=30
    pdf.setFont('Helvetica-Bold',9); pdf.drawString(48,y,'Pagamentos registrados'); y-=17; pdf.setFont('Helvetica',9)
    for p in payments: pdf.drawString(48,y,f"{p['created_at'][:10]} · {p['payment_method']} · R$ {p['amount']:.2f}"); y-=15
    if s['notes']: y-=10; pdf.drawString(48,y,f"Observações: {s['notes'][:110]}")
    pdf.setFont('Helvetica-Oblique',8); pdf.setFillColor(colors.grey); pdf.drawCentredString(w/2,45,'Documento de controle interno e comprovante de recebimento.'); pdf.save(); buf.seek(0)
    return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename=recibo_{sale_id:06d}.pdf'})

@app.get("/vendas/{sale_id}/carne")
def sale_booklet(sale_id: int):
    sale=query("SELECT s.*,c.name client_name,c.phone FROM sales s LEFT JOIN clients c ON c.id=s.client_id WHERE s.id=?",(sale_id,))
    if not sale: raise HTTPException(404,"Venda não encontrada")
    s=sale[0]; installments_rows=query("SELECT * FROM sale_installments WHERE sale_id=? ORDER BY installment_no",(sale_id,))
    if not installments_rows: raise HTTPException(404,"Esta venda não possui parcelas em aberto")
    buf=BytesIO(); pdf=canvas.Canvas(buf,pagesize=A4); w,h=A4
    pdf.setFillColor(colors.white); pdf.rect(0,h-145,w,145,fill=1,stroke=0); pdf.setStrokeColor(colors.HexColor('#b58b3b')); pdf.setLineWidth(1.4); pdf.line(42,h-149,w-42,h-149)
    logo=ROOT / 'static' / 'img' / 'logo-debora-ventura.png'
    if logo.exists(): pdf.drawImage(str(logo),w-145,h-132,width=110,height=110,preserveAspectRatio=True,mask='auto')
    pdf.setFillColor(colors.HexColor('#211f1a')); pdf.setFont('Helvetica-Bold',18); pdf.drawString(45,h-48,'DÉBORA VENTURA')
    pdf.setFillColor(colors.HexColor('#9a762f')); pdf.setFont('Helvetica',9); pdf.drawString(45,h-66,'STUDIO BEAUTY')
    pdf.setFillColor(colors.black); pdf.setFont('Helvetica-Bold',13); pdf.drawString(45,h-78,f"Carnê de pagamento · Venda nº {sale_id:06d}")
    pdf.setFont('Helvetica',10); pdf.drawString(45,h-100,f"Cliente: {s['client_name'] or 'Não identificado'}")
    pdf.drawString(45,h-116,f"Valor total: R$ {s['total']:.2f} · Já recebido: R$ {s['amount_paid']:.2f}")
    y=h-155
    for inst in installments_rows:
        pdf.setStrokeColor(colors.HexColor('#b69a63')); pdf.roundRect(42,y-73,w-84,66,7,stroke=1,fill=0)
        pdf.setFont('Helvetica-Bold',11); pdf.drawString(55,y-28,f"PARCELA {inst['installment_no']:02d}/{len(installments_rows):02d}")
        pdf.setFont('Helvetica-Bold',13); pdf.drawRightString(w-58,y-28,f"R$ {inst['amount']:.2f}")
        pdf.setFont('Helvetica',9); due=datetime.strptime(inst['due_date'],'%Y-%m-%d').strftime('%d/%m/%Y') if inst['due_date'] else 'A combinar'
        pdf.drawString(55,y-49,f"Vencimento: {due}    Situação: {inst['status']}    Pago: R$ {inst['amount_paid']:.2f} / R$ {inst['amount']:.2f}")
        pdf.drawRightString(w-58,y-49,'Data do pagamento: ____/____/______')
        y-=87
        if y<75: pdf.showPage(); y=h-55
    pdf.setFont('Helvetica-Oblique',8); pdf.setFillColor(colors.grey); pdf.drawCentredString(w/2,35,'Carnê para controle da cliente e do estabelecimento. Guarde os comprovantes de pagamento.')
    pdf.save(); buf.seek(0)
    return StreamingResponse(buf,media_type='application/pdf',headers={'Content-Disposition':f'inline; filename=carne_{sale_id:06d}.pdf'})


@app.post("/vendas/{sale_id}/receber")
def receive_payment(sale_id: int, amount: float = Form(...),
                    payment_method: str = Form("Pix"), note: str = Form("")):
    if amount <= 0:
        raise HTTPException(400, "O valor recebido deve ser maior que zero")
    def work(conn):
        sale = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not sale:
            raise HTTPException(404, "Venda não encontrada")
        balance = round(float(sale["total"]) - float(sale["amount_paid"]), 2)
        if amount > balance + 0.001:
            raise HTTPException(400, "O recebimento não pode superar o saldo em aberto")
        new_paid = round(float(sale["amount_paid"]) + amount, 2)
        status = "Pago" if new_paid >= float(sale["total"]) - 0.001 else "Parcial"
        conn.execute("UPDATE sales SET amount_paid=?,status=?,payment_method=? WHERE id=?",
                     (new_paid, status, payment_method, sale_id))
        conn.execute("UPDATE appointments SET amount_paid=?,payment_method=?,billing_status=? WHERE id=(SELECT appointment_id FROM sales WHERE id=?)",
                     (new_paid,payment_method,status,sale_id))
        conn.execute("INSERT INTO payments(sale_id,amount,payment_method,note) VALUES(?,?,?,?)",
                     (sale_id, amount, payment_method, note.strip()))
        remaining_payment=amount
        installments=conn.execute("SELECT * FROM sale_installments WHERE sale_id=? ORDER BY installment_no",(sale_id,)).fetchall()
        for inst in installments:
            due=max(0.0,float(inst["amount"])-float(inst["amount_paid"]))
            applied=min(due,remaining_payment)
            if applied>0:
                paid_now=round(float(inst["amount_paid"])+applied,2)
                state="Pago" if paid_now>=float(inst["amount"])-.001 else "Parcial"
                conn.execute("UPDATE sale_installments SET amount_paid=?,status=?,paid_at=? WHERE id=?",(paid_now,state,datetime.now().isoformat(timespec="minutes"),inst["id"]))
                remaining_payment=round(remaining_payment-applied,2)
            if remaining_payment<=.001: break
        sale_type=conn.execute("SELECT item_type FROM sale_items WHERE sale_id=? LIMIT 1",(sale_id,)).fetchone()
        cash_type="produtos" if sale_type and sale_type["item_type"]=="produto" else "servicos"
        conn.execute("""INSERT INTO cash_movements
            (cash_type,direction,amount,description,payment_method,status,sale_id)
            VALUES(?,'Entrada',?,?,?,'Recebido',?)""",
            (cash_type,amount,note.strip() or f"Recebimento da venda #{sale_id}",payment_method,sale_id))
        pending=round(float(sale["total"])-new_paid,2)
        conn.execute("""UPDATE cash_movements SET amount=?,status='Pendente'
            WHERE sale_id=? AND status='Pendente'""",(pending,sale_id))
        conn.execute("DELETE FROM cash_movements WHERE sale_id=? AND status='Pendente' AND amount<=0.001",(sale_id,))
        return sale["client_id"]
    client_id = transaction(work)
    return RedirectResponse(f"/clientes/{client_id}" if client_id else "/vendas", status_code=303)


@app.get("/caixa", response_class=HTMLResponse)
def cash_home():
    return RedirectResponse("/caixa/geral", status_code=303)

@app.get("/caixa/{cash_type}", response_class=HTMLResponse)
def cash_page(request: Request, cash_type: str, start: str = "", end: str = ""):
    if cash_type not in ("geral", "servicos", "produtos"):
        raise HTTPException(404, "Fluxo de caixa não encontrado")
    start = start or date.today().replace(day=1).isoformat()
    end = end or date.today().isoformat()
    category_sql = "" if cash_type == "geral" else " AND cash_type=?"
    params = (start, end) if cash_type == "geral" else (start, end, cash_type)
    totals = query(f"""SELECT
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Recebido' THEN amount ELSE 0 END),0) entradas,
        COALESCE(SUM(CASE WHEN direction='Saída' AND status='Recebido' THEN amount ELSE 0 END),0) saidas,
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Pendente' THEN amount ELSE 0 END),0) pendente
        FROM cash_movements WHERE ((substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?) OR status='Pendente'){category_sql}""", params)[0]
    rows = query(f"""SELECT * FROM cash_movements WHERE
        ((substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?) OR status='Pendente'){category_sql}
        ORDER BY CASE WHEN status='Pendente' THEN 0 ELSE 1 END, created_at DESC LIMIT 500""", params)
    split = query("""SELECT cash_type,
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Recebido' THEN amount ELSE 0 END),0) entradas,
        COALESCE(SUM(CASE WHEN direction='Saída' AND status='Recebido' THEN amount ELSE 0 END),0) saidas
        FROM cash_movements WHERE substr(created_at,1,10)>=? AND substr(created_at,1,10)<=? GROUP BY cash_type""", (start,end))
    split_map = {r['cash_type']: dict(r) for r in split}
    return render(request, "cash.html", cash_type=cash_type, start=start, end=end, totals=totals, rows=rows,
                  split=split_map, page_date=date.today().strftime("%d/%m/%Y"))

@app.post("/caixa/{cash_type}/lancamento")
def cash_entry(cash_type: str, direction: str = Form(...), amount: float = Form(...),
               description: str = Form(...), payment_method: str = Form("Dinheiro"),
               cash_category: str = Form("servicos")):
    if cash_type not in ("geral", "servicos", "produtos") or direction not in ("Entrada","Saída"):
        raise HTTPException(400,"Tipo de lançamento inválido")
    if amount <= 0 or not description.strip():
        raise HTTPException(400,"Informe valor e descrição")
    target_category = cash_category if cash_type == "geral" else cash_type
    if target_category not in ("servicos", "produtos"):
        raise HTTPException(400, "Selecione a categoria do lançamento")
    execute("""INSERT INTO cash_movements
        (cash_type,direction,amount,description,payment_method,status)
        VALUES(?,?,?,?,?,'Recebido')""",
        (target_category,direction,amount,description.strip(),payment_method))
    return RedirectResponse("/caixa/geral" if cash_type == "geral" else f"/caixa/{cash_type}",status_code=303)

@app.get("/relatorios", response_class=HTMLResponse)
def reports_page(request: Request, start: str = "", end: str = "", view: str = "geral"):
    start = start or date.today().replace(day=1).isoformat()
    end = end or date.today().isoformat()
    if start > end:
        raise HTTPException(400, "Período inválido")
    period = ("substr(created_at,1,10) >= ? AND substr(created_at,1,10) <= ?", (start,end))
    totals = query(f"""SELECT COALESCE(SUM(total),0) total, COALESCE(SUM(amount_paid),0) received,
        COALESCE(SUM(total-amount_paid),0) pending, COUNT(*) sales_count FROM sales WHERE {period[0]}""", period[1])[0]
    product_totals = query("""SELECT COALESCE(SUM(si.quantity),0) units,
        COALESCE(SUM(si.line_total),0) revenue, COALESCE(SUM(si.quantity*si.unit_cost),0) cost,
        COALESCE(SUM(si.line_total-si.quantity*si.unit_cost),0) profit
        FROM sale_items si JOIN sales s ON s.id=si.sale_id
        WHERE si.item_type='produto' AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?""", (start,end))[0]
    debtors = query("""SELECT c.id,c.name,c.phone,COUNT(s.id) sale_count,
        SUM(s.total-s.amount_paid) debt FROM clients c JOIN sales s ON s.client_id=c.id
        WHERE s.total>s.amount_paid AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?
        GROUP BY c.id ORDER BY debt DESC""", (start,end))
    paid = query("""SELECT s.*,c.name client_name FROM sales s LEFT JOIN clients c ON c.id=s.client_id
        WHERE s.total>0 AND s.total=s.amount_paid AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?
        ORDER BY s.created_at DESC""", (start,end))
    product_sales = query("""SELECT si.description, SUM(si.quantity) quantity, SUM(si.line_total) revenue,
        SUM(si.quantity*si.unit_cost) cost, SUM(si.line_total-si.quantity*si.unit_cost) profit
        FROM sale_items si JOIN sales s ON s.id=si.sale_id
        WHERE si.item_type='produto' AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?
        GROUP BY si.description ORDER BY quantity DESC""", (start,end))
    stock = query("""SELECT p.*, COALESCE(SUM(CASE WHEN sm.movement_type='Venda' THEN -sm.quantity ELSE sm.quantity END),0) movements
        FROM products p LEFT JOIN stock_movements sm ON sm.product_id=p.id GROUP BY p.id ORDER BY p.name""")
    return render(request, "reports.html", start=start, end=end, view=view, totals=totals,
                  product_totals=product_totals, debtors=debtors, paid=paid,
                  product_sales=product_sales, stock=stock)

@app.get("/relatorios/servicos", response_class=HTMLResponse)
def service_financial_report(request: Request, start: str = "", end: str = ""):
    return category_financial_report(request, "servicos", start, end)

@app.get("/relatorios/produtos", response_class=HTMLResponse)
def product_financial_report(request: Request, start: str = "", end: str = ""):
    return category_financial_report(request, "produtos", start, end)

def category_financial_report(request: Request, category: str, start: str, end: str):
    start = start or date.today().replace(day=1).isoformat()
    end = end or date.today().isoformat()
    if start > end:
        raise HTTPException(400, "Período inválido")
    totals = query("""SELECT
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Recebido' THEN amount ELSE 0 END),0) received,
        COALESCE(SUM(CASE WHEN direction='Saída' AND status='Recebido' THEN amount ELSE 0 END),0) expenses,
        COALESCE(SUM(CASE WHEN direction='Entrada' AND status='Pendente' THEN amount ELSE 0 END),0) pending,
        COUNT(*) movements
        FROM cash_movements WHERE cash_type=? AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?""", (category,start,end))[0]
    rows = query("""SELECT * FROM cash_movements WHERE cash_type=? AND
        ((substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?) OR status='Pendente')
        ORDER BY created_at DESC LIMIT 500""", (category,start,end))
    if category == "servicos":
        operational = query("""SELECT COUNT(*) count, COALESCE(SUM(total_amount),0) billed,
            COALESCE(SUM(amount_paid),0) received FROM appointments
            WHERE substr(starts_at,1,10)>=? AND substr(starts_at,1,10)<=?""", (start,end))[0]
        label = "Serviços e atendimentos"
    else:
        operational = query("""SELECT COUNT(DISTINCT s.id) count, COALESCE(SUM(si.line_total),0) billed,
            COALESCE(SUM(si.quantity),0) units FROM sales s JOIN sale_items si ON si.sale_id=s.id
            WHERE si.item_type='produto' AND substr(s.created_at,1,10)>=? AND substr(s.created_at,1,10)<=?""", (start,end))[0]
        label = "Vendas de produtos"
    return render(request, "financial_report.html", category=category, label=label, start=start, end=end,
                  totals=totals, rows=rows, operational=operational, page_date=date.today().strftime("%d/%m/%Y"))

@app.get("/relatorios/estoque", response_class=HTMLResponse)
def stock_report(request: Request):
    movements = query("""SELECT sm.*,p.name product_name FROM stock_movements sm
        JOIN products p ON p.id=sm.product_id ORDER BY sm.created_at DESC LIMIT 300""")
    return render(request, "stock_report.html", movements=movements)
