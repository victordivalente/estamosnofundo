# Estamos No Fundo?

Página estática que mostra só **SIM** ou **NÃO** e o crédito «Um Oferecimento De» com o logotipo da [Open-Source Law+](https://www.opensourcelaw.com.br). Os indicadores clássicos do Bitcoin rodam em `scripts/build.py` e ficam em `data/latest.json`, não na página.

Site: https://victordivalente.github.io/estamosnofundo/

O visual de `index.html` não deve ser alterado sem pedido explícito do Victor.

## Hospedagem

GitHub Pages, fonte GitHub Actions. O workflow [`.github/workflows/pages.yml`](.github/workflows/pages.yml) roda `scripts/build.py` (só a biblioteca padrão do Python, sem `requirements.txt`) todos os dias às 09:00 UTC (06:00 no horário de Brasília), e também em push à `main` e por disparo manual. Ele publica só `index.html`, `assets/` e `data/latest.json`.

O domínio `estamosnofundo.com.br` só entra depois de registrado e ativo. Não configurar ainda o custom domain nem o DNS.

## Como rodar localmente

```bash
python3 scripts/build.py
python3 -m http.server 8080 --directory .
```

Abra `http://127.0.0.1:8080/`. Sem chave de API. Se o JSON não for servido, a resposta já está gravada no HTML.
