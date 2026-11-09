# Studio Débora Ventura — v1.4.3

Atualização focada na agenda e no uso em dispositivos móveis.

## Alterações

- Busca de clientes com sugestões próprias, compatível com toque em celulares e tablets.
- Busca por nome completo, trecho do nome ou iniciais (por exemplo, “LA” para Lucas Alves).
- A seleção da cliente envia o ID correto ao formulário; texto digitado sem selecionar uma cliente não é aceito em campos obrigatórios.
- No novo agendamento, o campo Serviço começa em branco com “Selecione um serviço”; o usuário precisa escolher.
- Ajustes de acessibilidade e tamanho dos controles para toque em telas pequenas.
- Mantém as rotas, os modelos de dados e as funcionalidades financeiras existentes.

## Publicação

Esta versão não solicita alteração de senha, segredo de sessão ou configuração do banco de dados. Preserve as variáveis de ambiente já existentes no serviço Render.

1. Faça backup/exportação dos dados antes de publicar qualquer atualização.
2. Atualize os arquivos do mesmo repositório GitHub e branch já conectados ao serviço Render.
3. Aguarde o deploy terminar e confirme `/health` retornando a versão `1.4.3`.
4. Teste no computador e em um celular: selecionar cliente, criar agendamento com serviço escolhido, editar agendamento, cadastrar cliente e salvar uma edição.
5. Confirme que cada registro aparece novamente ao sair e entrar no sistema.

Não crie outro serviço Render, não use Blueprint e não altere variáveis do banco nesta atualização.
