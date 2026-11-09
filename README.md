# Studio Débora Ventura — v1.4.0

Sistema de gestão do Studio Débora Ventura (FastAPI), com SQLite para uso local e PostgreSQL para hospedagem online.

## O que muda nesta versão

- O sistema online passa a usar PostgreSQL externo por meio da variável `DATABASE_URL`.
- Em produção, o sistema bloqueia a inicialização se `DATABASE_URL` não estiver configurada. Isso impede que a aplicação volte silenciosamente a gravar em um SQLite temporário e dê a impressão de que os cadastros foram apagados.
- O SQLite continua disponível para execução local no computador.
- Mantém os ajustes de formulários e cadastros da versão 1.3.0.

## Importante sobre os cadastros existentes

Esta atualização cria e mantém as tabelas no PostgreSQL, mas **não consegue recuperar automaticamente dados que já desapareceram do armazenamento temporário do Render**. Antes de qualquer migração de dados, é necessário localizar e obter uma cópia do banco antigo, caso ainda exista. Não apague nem substitua nenhum arquivo de banco antigo que possa ser encontrado.

## Configuração online — GitHub + Render + Neon

1. Crie um projeto PostgreSQL no Neon usando o plano Free.
2. No painel do Neon, copie a connection string PostgreSQL (use a opção pooled connection se estiver disponível). Ela deve começar com `postgresql://` e conter `sslmode=require`.
3. No Render, abra o serviço web já existente do Studio e entre em **Environment**.
4. Configure estas variáveis:
   - `APP_ENV` = `production`
   - `DATABASE_URL` = connection string copiada do Neon (segredo; nunca coloque no GitHub ou em mensagens)
   - `STUDIO_MASTER_PASSWORD` = mantenha a senha forte já configurada
   - `SESSION_SECRET_KEY` = mantenha o segredo já configurado
5. Salve as variáveis. Antes de publicar esta versão, confirme que `DATABASE_URL` está salva corretamente no Render.
6. Envie os arquivos desta versão ao mesmo repositório GitHub conectado ao Render. O Render fará o deploy.
7. Depois do deploy, abra o endereço do Studio e faça testes controlados: cadastrar um cliente de teste, sair, entrar novamente, reiniciar/publicar e confirmar que o cliente continua lá. Depois remova o cadastro de teste.

**Nunca compartilhe a connection string `DATABASE_URL` em prints, mensagens ou no repositório.** Se ela aparecer em algum print, troque a senha do banco no Neon.

## Limites do Neon Free

O plano gratuito tem limites de armazenamento e processamento e suspende o processamento após inatividade; quando isso ocorre, o banco volta a iniciar quando recebe uma nova conexão. A suspensão não apaga os dados. Acompanhe o uso e faça backups periódicos. Para operação comercial crítica, considere futuramente um plano com backups e garantias adicionais.

## Execução local no Windows

1. Execute `INICIAR_LOCAL.bat`.
2. Acesse `http://127.0.0.1:8000`.
3. Sem `DATABASE_URL`, o sistema usa SQLite em `data/studio.db`.

Não envie `data/`, arquivos `.db`, `.env`, senhas ou tokens ao GitHub.
