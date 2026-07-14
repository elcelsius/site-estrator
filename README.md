# Extrator de Sites

[![Testes](https://github.com/elcelsius/site-estrator/actions/workflows/tests.yml/badge.svg)](https://github.com/elcelsius/site-estrator/actions/workflows/tests.yml)
[![Licença MIT](https://img.shields.io/badge/licen%C3%A7a-MIT-blue.svg)](LICENSE)

Aplicação em Python para rastrear páginas web, extrair o conteúdo principal e
salvá-lo em arquivos de texto. O projeto também inclui utilitários opcionais
para sites com menus dinâmicos, conversão de PDFs, deduplicação e refino de
documentos Markdown.

As configurações ficam na interface, em argumentos de linha de comando ou em
variáveis de ambiente. O repositório não contém URL, domínio, credencial ou
nome de organização específico.

## Recursos

- interface gráfica em Tkinter;
- rastreamento por profundidade real, com limite de páginas;
- início, acompanhamento e interrupção pela interface;
- progresso e logs exibidos na própria janela;
- saída em `.txt` pela interface e opção de Markdown pela CLI;
- download de PDFs encontrados durante o rastreamento;
- respeito a `robots.txt`, intervalo entre requisições e `User-Agent`
  configurável;
- suporte adicional a árvores AJAX/PrimeFaces por seletores;
- conversão e validação de documentos para pipelines de recuperação de
  informação.

## Requisitos

- Python 3.10 ou superior;
- Tkinter, normalmente incluído na instalação oficial do Python;
- Chromium instalado pelo Playwright.

## Instalação

Clone o repositório, entre na pasta do projeto e crie um ambiente virtual:

```bash
python -m venv .venv
```

Ative o ambiente no Windows:

```powershell
.\.venv\Scripts\Activate.ps1
```

No Linux ou macOS:

```bash
source .venv/bin/activate
```

Instale as dependências e o navegador:

```bash
python -m pip install -r requirements.txt
playwright install chromium
```

## Uso da interface gráfica

Execute:

```bash
python app.py
```

Na janela:

1. informe a URL inicial completa, incluindo `https://`;
2. escolha a profundidade do rastreamento;
3. selecione a pasta que receberá os arquivos `.txt`;
4. clique em **Iniciar**;
5. acompanhe o progresso e os logs ou clique em **Parar**.

Profundidade `0` processa somente a URL inicial. Profundidade `1` também
processa os links encontrados nela, e assim sucessivamente. O encerramento é
cooperativo: ao solicitar a parada, a navegação que estiver em curso termina
antes de o navegador ser fechado.

## Configuração

Copie [`.env.example`](.env.example) para `.env` se quiser alterar os padrões.
No PowerShell:

```powershell
Copy-Item .env.example .env
```

As principais opções são:

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `DEFAULT_BASE_URL` | `https://example.com` | URL inicial sugerida |
| `MAX_DEPTH` | `2` | Profundidade máxima |
| `MAX_PAGES` | `500` | Limite de páginas por execução |
| `OUTPUT_DIR` | `data/0_extracao_bruta` | Pasta de documentos |
| `PDF_OUTPUT_DIR` | `pdf/originais` | Pasta de PDFs |
| `SAME_DOMAIN_ONLY` | `true` | Restringe o rastreamento ao domínio inicial |
| `RESPECT_ROBOTS_TXT` | `true` | Aplica as regras publicadas pelo site |
| `POLITENESS_DELAY` | `0.6` | Intervalo mínimo entre páginas, em segundos |
| `CRAWLER_USER_AGENT` | `SiteExtractor/1.0` | Identificação enviada ao servidor |

Não versione o arquivo `.env`. Ele está listado no `.gitignore`.

## Linha de comando

Para extrair texto:

```bash
python extrator.py --base-url https://example.com --max-depth 2 \
  --output-dir ./saida --output-format txt
```

Para gerar Markdown com front matter:

```bash
python extrator.py --base-url https://example.com --max-depth 1 \
  --output-format md
```

Use `python extrator.py --help` para ver todas as opções.

### Menus dinâmicos

O extrator de árvores não possui seletores vinculados a um site. Informe os
seletores da página que será processada:

```bash
python extrator_menu_dinamico.py \
  --url https://example.com \
  --content-selector "main" \
  --item-selector ".ui-treenode-label"
```

Há opções adicionais para o seletor dos expansores, título, pré-clique e
dropdown. Consulte `python extrator_menu_dinamico.py --help`.

## Utilitários opcionais

Converter PDFs locais em Markdown:

```bash
python transforma_pdfs_md.py
```

Inferir metadados de atos normativos sem definir instituição ou fonte:

```bash
python metadados_documentos.py --input-dir data/0_extracao_bruta
```

Validar metadados:

```bash
python validador_metadados.py --root data/0_extracao_bruta
```

Refinar e deduplicar localmente:

```bash
python refinador.py --provider none
```

O refino com Google é opcional. Instale suas dependências separadamente e
preencha a chave somente no `.env` local:

```bash
python -m pip install -r requirements-llm.txt
python refinador.py --provider google
```

Também é possível usar uma instância local do Ollama com as variáveis descritas
em `.env.example`.

## Desenvolvimento

Instale as ferramentas e execute as verificações:

```bash
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
```

A integração contínua executa as mesmas verificações a cada `push` e `pull
request`.

## Uso responsável

Antes de rastrear um site, confira seus termos de uso e a legislação aplicável.
Mantenha o respeito a `robots.txt` habilitado e ajuste o intervalo entre
requisições para não sobrecarregar o servidor. Você é responsável pelas URLs
informadas e pelos dados coletados.

## Segurança

O fluxo de integração contínua procura padrões de segredo e audita as
dependências conhecidas antes de aceitar a versão publicada. Credenciais devem
existir somente no `.env` local e precisam ser revogadas imediatamente se forem
expostas. Consulte [SECURITY.md](SECURITY.md) para relatar uma vulnerabilidade.

## Contribuição e licença

Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para preparar uma contribuição e
[SECURITY.md](SECURITY.md) para relatar vulnerabilidades. O projeto é
distribuído sob a licença MIT, disponível em [LICENSE](LICENSE).
