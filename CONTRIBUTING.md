# Como contribuir

Contribuições são bem-vindas. Antes de abrir um pull request, procure uma issue
relacionada ou descreva claramente o problema que a mudança resolve.

## Preparação

1. Crie um ambiente virtual com Python 3.10 ou superior.
2. Instale `requirements-dev.txt`.
3. Crie uma branch curta e focada.
4. Não inclua `.env`, credenciais, conteúdo coletado ou dados de terceiros.

## Qualidade

Execute antes de enviar:

```bash
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
```

Novas funções e classes devem ter docstrings. Comentários devem ser escritos em
português e explicar apenas decisões ou comportamentos que não sejam evidentes
pelo código.

## Pull requests

Mantenha a alteração pequena, inclua testes para novos comportamentos e descreva
eventuais impactos de compatibilidade. Não adicione seletores, domínios ou
valores de configuração que atendam somente a uma organização; prefira opções
de CLI ou variáveis de ambiente.
