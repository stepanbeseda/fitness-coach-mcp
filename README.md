# fitness-coach-mcp

Self-hosted MCP servery pro osobního fitness kouče v Claude:

- **Hevy**: historie silových tréninků, rutiny, šablony cviků, zápis rutin a workoutů
  (upstream [`chrisdoc/hevy-mcp`](https://github.com/chrisdoc/hevy-mcp) 6.1.16).
- **Garmin Connect**: HRV, spánek, Body Battery, training readiness, aktivity,
  výkonnostní metriky (předpovědi závodů, laktátový práh, tepové a výkonové zóny, VO2max), osobní
  rekordy, vybavení, tělesné složení (vážení, % tuku), výživa a hydratace, ruční zápis váhy a tvorba
  strukturovaných workoutů naplánovaných do hodinek (vlastní server `garmin-mcp/` nad
  [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) 0.3.17).
- **Kouč (paměť)**: cíle, omezení (zranění, dostupné dny, vybavení), tréninkový plán po dnech se stavem
  a deník poznámek, aby si kouč pamatoval stav mezi konverzacemi (vlastní server `coach-mcp/` se SQLite).
  Volitelně ráno pošle briefing s verdiktem a upozorněními do tvého chatu s Telegram botem (viz Provoz).

Všechny tři běží za jednou OAuth 2.1 gateway a ven jsou vystavené jen přes **vlastní Tailscale uzel**:
Funnel na portu 443 pro claude.ai (web, mobil, Desktop) a tailnet pro Claude Code / lokální Claude Desktop.

```
claude.ai / Desktop / mobil ──(cloud Anthropicu, 160.79.104.0/21)──HTTPS:443──┐
                                                                              │ Tailscale Funnel
Claude Code / mcp-remote (Mac, tailnet) ──WireGuard──────────────────────────┤
                                                                              ▼
 ┌─ ts ─────────── tailscale/tailscale v1.102.5, uzel fitness-mcp (tag:fitness-mcp), userspace
 │                 serve + funnel :443  →  127.0.0.1:8080
 │   (sdílený network namespace, jen loopback)
 ├─ gateway ────── OAuth 2.1 AS + resource server + reverse proxy + audit + rate limit (Python)
 │                   /hevy/mcp   → hevy-mcp:3000/mcp
 │                   /garmin/mcp → garmin-mcp:8000/mcp
 │                   /coach/mcp  → coach-mcp:8000/mcp
 ├─ hevy-mcp ───── oficiální image (Streamable HTTP), privátní síť "mcp"
 ├─ garmin-mcp ─── vlastní server, tokeny ve volume garmin-tokens, privátní síť "mcp"
 └─ coach-mcp ──── vlastní server, SQLite ve volume coach-data, privátní síť "mcp"
                   ranní briefing → Telegram Bot API (HTTPS ven, jen když je nastavený)
```

URL konektorů (stejné z claude.ai i z tailnetu, MagicDNS v tailnetu vrací 100.x adresu). `<tailnet>` je
DNS přípona tvého tailnetu: `tailscale status --json | jq -r .MagicDNSSuffix`, např. `tail1234ab.ts.net`.

- `https://fitness-mcp.<tailnet>/hevy/mcp`
- `https://fitness-mcp.<tailnet>/garmin/mcp`
- `https://fitness-mcp.<tailnet>/coach/mcp`

Proč vlastní uzel a ne Funnel na hostu: claude.ai volá konektory jen na port 443 a Funnel se zapíná pro
celý port. Vlastní uzel nechá cokoli, co host na :443 už servíruje (třeba jen do tailnetu), netknuté, a
gateway nemusí publikovat žádný port na hostu.

## Bezpečnostní model

| Vrstva | Co dělá |
|---|---|
| Síť | Žádný publikovaný port na hostu. Gateway poslouchá jen na 127.0.0.1 v namespace sidecaru, takže jediná cesta dovnitř je `tailscale serve`. Backendy jsou jen v privátní Docker síti; ven volají jen svá API (Hevy, Garmin Connect) a `coach-mcp` volitelně Telegram Bot API (`api.telegram.org`) pro ranní briefing (viz Provoz → Ranní briefing). Ostatní služby hostu ani jeho vlastní `tailscale serve` se nemění. |
| Funnel | Veřejně jde jen discovery (`/.well-known/*`), odpověď 401 a `/token`. Cokoli s tokenem a `/token` přes Funnel projde jen z `160.79.104.0/21` (publikovaný odchozí rozsah Anthropicu pro MCP konektory). |
| Přihlášení | `/authorize` (heslo argon2id + TOTP) jen z tvých zařízení v tailnetu (`Tailscale-User-Login` = `OWNER_TAILSCALE_LOGIN`, ne přes Funnel). Lockout s exponenciálním backoffem, ochrana proti znovupoužití TOTP kódu, CSRF cookie. |
| OAuth | MCP auth spec 2025-11-25: PKCE S256 povinně, `resource` (RFC 8707), takže každý token platí jen pro jeden endpoint (Hevy token na Garminu nefunguje). `iss` (RFC 9207). Access token 1 h, refresh 90 dní, rotace s detekcí znovupoužití (s oknem 120 s na retry). Revokace (RFC 7009). |
| Klienti | Žádná otevřená registrace (DCR). Povolení klienti jsou v `gateway/config.toml`: Claude (claude.ai, CIMD), Claude Code (CIMD) a `mcp-remote`. CIMD dokumenty jsou připnuté (žádný runtime fetch, žádné SSRF), `healthcheck.sh` hlídá změny. |
| Backendy | Klientský token se nikdy nepřeposílá. Gateway posílá vlastní interní token pro každý backend (`secrets/*_backend_token`). Hevy nástroj `feedback` (posílá report autorovi) je zablokovaný. |
| Tokeny | Opaque 256 bit, v SQLite jen jejich SHA-256 hash. |
| Kontejnery | Non-root (uid 10001), `read_only`, `cap_drop: ALL`, `no-new-privileges`, limity paměti a procesů, healthchecky, rotace logů. Image pinnuté `tag@sha256`, Python závislosti zamčené s hashi. |
| Secrets | `secrets/` (0700, soubory 0640 se skupinou 10001), Docker secrets, nikdy v gitu, env ani logu. |
| Audit | `/data/audit.jsonl` + stdout: přihlášení, vydání/obnova/revokace tokenů, každé MCP volání (metoda, nástroj, výsledek, doba). Nikdy argumenty, výsledky, tokeny ani hesla. |

## Instalace

Předpoklady:

- Linux server s Dockerem a Compose v2 (testováno na Ubuntu 24.04, linux/arm64 – Oracle Cloud Ampere;
  všechny base image jsou multi-arch), připojený do tailnetu.
- Tailscale účet se zapnutým MagicDNS a HTTPS certifikáty (admin konzole → DNS).
- Claude plán s custom konektory, Hevy PRO (kvůli API klíči), Garmin Connect účet.
- Na Macu/PC, ze kterého konektory připojuješ, zapnutý Tailscale.

### 0. Klon

```bash
git clone <url-tohoto-repa> fitness-coach-mcp && cd fitness-coach-mcp
```

Všechny další příkazy se spouští z adresáře repa.

### 1. Tailscale policy (admin konzole → Access controls)

Přidej tag pro nový uzel a povol mu Funnel. Tagované uzly nespadají pod `autogroup:member`, proto
explicitní `nodeAttrs`:

```jsonc
"tagOwners": {
  "tag:fitness-mcp": ["autogroup:admin"]
},
"nodeAttrs": [
  // ...stávající položky nech být...
  { "target": ["tag:fitness-mcp"], "attr": ["funnel"] }
]
```

Pokud tvoje policy nepovoluje „vše všem“, přidej i přístup ze svých zařízení:
`{"src": ["autogroup:member"], "dst": ["tag:fitness-mcp"], "ip": ["tcp:443"]}` do `grants`.

### 2. Auth key

Admin konzole → Settings → Keys → **Generate auth key**: Reusable **ne**, Ephemeral **ne**,
Pre-approved **ano**, Tags **`tag:fitness-mcp`**. Klíč je potřeba jen pro první přihlášení uzlu (u tagovaných
uzlů je expirace klíče vypnutá, stav drží volume `ts-state`).

### 3. Konfigurace

```bash
cp .env.example .env && chmod 600 .env
```

V `.env` vyplň `PUBLIC_BASE_URL` (`https://fitness-mcp.<tailnet>`) a `OWNER_TAILSCALE_LOGIN` (tvůj
Tailscale login, např. `jane@github`). Příkazy, které ti obě hodnoty vypíšou, jsou přímo v `.env.example`.
Volitelně ranní briefing do Telegramu: `COACH_TELEGRAM_CHAT_ID` a `COACH_BRIEFING_*` (viz Provoz → Ranní briefing).

### 4. Secrets

```bash
scripts/setup-secrets.sh
```

Skript se zeptá (skrytý vstup) na auth key, Hevy API key (hned ho ověří proti Hevy API), heslo pro
gateway a nastaví TOTP (QR kód v terminálu + ověření kódem). Interní tokeny mezi gateway a backendy
vygeneruje sám.

### 5. Start

```bash
docker compose up -d --build
docker compose ps          # všechny služby healthy
```

Uzel `fitness-mcp` se objeví v admin konzoli s tagem `tag:fitness-mcp`, Funnel se zapne sám ze
`tailscale/serve.json` (`AllowFunnel`).

### 6. Garmin login (jednorázově, interaktivně přes SSH)

```bash
scripts/garmin-login.sh            # e-mail, heslo, MFA kód; heslo se neukládá
scripts/garmin-login.sh --check
```

### 7. Ověření

```bash
scripts/healthcheck.sh             # kontejnery, uzel, Funnel, discovery, login jen z tailnetu, Garmin, Hevy, databáze kouče
scripts/smoke-test.sh              # tools/list na všech třech endpointech, souhrn paměti kouče, 3 poslední Hevy workouty, dnešní HRV/spánek, předpovědi závodů a tepové zóny
```

## Funnel

- Zapnutý je deklarativně v `tailscale/serve.json` (`"AllowFunnel": {"${TS_CERT_DOMAIN}:443": true}`)
  a jen na uzlu `fitness-mcp`. Na hostu se nic nemění.
- Stav: `docker compose exec ts tailscale --socket /tmp/tailscaled.sock funnel status`
- Vypnutí (jen tailnet, claude.ai pak konektory neuvidí): smaž blok `AllowFunnel` ze `serve.json`
  (sidecar soubor sleduje, změna se projeví sama).
- Veřejné DNS jméno uzlu je dohledatelné (Certificate Transparency), s čímž model počítá: veřejně nic
  citlivého není.

## Přidání konektorů

### claude.ai (web, platí i pro mobil a Desktop)

1. Na Macu (nebo telefonu) **se zapnutým Tailscale** otevři claude.ai → Settings → Connectors →
   **Add custom connector**.
2. Název `Hevy`, URL `https://fitness-mcp.<tailnet>/hevy/mcp`, OAuth Client ID/Secret nech
   prázdné (claude.ai se identifikuje přes CIMD) → **Add** → **Connect**.
3. Otevře se přihlašovací stránka gateway: zadej heslo + TOTP kód → **Povolit**.
4. Totéž pro `Garmin`, URL `https://fitness-mcp.<tailnet>/garmin/mcp`.
5. Totéž pro `Kouč`, URL `https://fitness-mcp.<tailnet>/coach/mcp`.
6. V projektu „Fitness kouč“ všechny tři konektory zapni. Custom Instructions jsou v
   [`docs/coach-instructions.md`](docs/coach-instructions.md).

Konektory se automaticky objeví v Claude Desktop i v mobilní aplikaci (volání jdou vždy z cloudu
Anthropicu přes Funnel). Přihlašovat se je potřeba jen při prvním připojení, případně když vyprší refresh
token (90 dní bez použití) nebo když grant zrušíš.

### Claude Code (přes tailnet)

```bash
claude mcp add --transport http hevy   https://fitness-mcp.<tailnet>/hevy/mcp
claude mcp add --transport http garmin https://fitness-mcp.<tailnet>/garmin/mcp
claude mcp add --transport http coach  https://fitness-mcp.<tailnet>/coach/mcp
# v Claude Code: /mcp → vyber server → Authenticate (otevře prohlížeč s loginem gateway)
```

Claude Code se identifikuje přes CIMD a používá callback `http://localhost:<port>/callback` (povoleno
v `gateway/config.toml`). Funguje jen se zapnutým Tailscale; tokeny z veřejného internetu mimo rozsah
Anthropicu gateway odmítne.

### Claude Desktop lokálně přes tailnet (bez cloudu Anthropicu)

Konektory z claude.ai fungují v Desktopu samy. Pokud chceš spojení čistě přes tailnet, přidej do
`~/Library/Application Support/Claude/claude_desktop_config.json` (vyžaduje Node.js na Macu):

```json
{
  "mcpServers": {
    "hevy-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/hevy/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/hevy/mcp"]
    },
    "garmin-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/garmin/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/garmin/mcp"]
    },
    "coach-tailnet": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "https://fitness-mcp.<tailnet>/coach/mcp",
               "--static-oauth-client-info", "{\"client_id\":\"mcp-remote\"}",
               "--resource", "https://fitness-mcp.<tailnet>/coach/mcp"]
    }
  }
}
```

`mcp-remote` je pinnutý (repo v srpnu 2026 změnilo správce). Po restartu Desktopu se otevře login gateway.

## Provoz

| Úkol | Příkaz |
|---|---|
| Stav celé služby | `scripts/healthcheck.sh` (pro cron: `--quiet`, exit 1 = problém) |
| Logy | `docker compose logs -f gateway` (bez query stringů a tokenů) |
| Audit | `scripts/admin.sh audit 100` |
| Připojení klienti (granty) | `scripts/admin.sh grants list` |
| Odpojit klienta | `scripts/admin.sh grants revoke <id>` (nebo `--all`) |
| Rotace Hevy klíče | vygeneruj nový v Hevy → `scripts/setup-secrets.sh hevy-key` (ověří a restartuje hevy-mcp) |
| Změna hesla / nový TOTP | `scripts/setup-secrets.sh password` / `scripts/setup-secrets.sh totp` |
| Rotace interních tokenů | `scripts/setup-secrets.sh backend-tokens` |
| Ranní briefing ručně | `docker compose exec coach-mcp python -m coach_mcp send-briefing` |
| Telegram bot | `scripts/setup-secrets.sh telegram-token` (token od @BotFather, vypíše chat id), `scripts/setup-secrets.sh telegram-chat-id` (chat id znovu) |
| Nové verze | `scripts/check-updates.sh` |
| Test bez dopadu na provoz | `scripts/e2e-test.sh` (samostatný compose projekt, bez internetu a Tailscale) |

Doporučený cron (hlášení jen při problému):

```cron
17 7 * * * cd /cesta/k/fitness-coach-mcp && scripts/healthcheck.sh --quiet
```

### Ranní briefing (Telegram)

`coach-mcp` umí jednou denně poslat do tvého chatu s vlastním Telegram botem krátký briefing: verdikt zelená /
žlutá / červená podle pravidel ze sekce 2 `docs/coach-instructions.md`, čísla za ním (readiness, HRV vs.
baseline, spánek, Body Battery), dnešní plán z paměti kouče a nová upozornění (HRV pod baseline 3+ noci,
klidová tepovka o 5+ bpm nad 7denním průměrem, zátěž mimo optimální rozsah Garminu, včerejší nesplněný
trénink, neplatné přihlášení do Garminu). Počítá se bez LLM; nástroj `get_daily_briefing` ho počítá stejně
z aktuálních dat Garminu.

**Soukromí:** zpráva obsahuje zdravotní údaje. Odchází z tvého serveru i tailnetu na servery Telegramu
a zůstává v historii chatu, dokud ji nesmažeš; chaty s boty nejsou šifrované end-to-end. Token bota
(`secrets/telegram_bot_token`) dává nad botem plnou kontrolu: při úniku ho v @BotFather zneplatni a nový
ulož přes `scripts/setup-secrets.sh telegram-token`.

Zapnutí:

1. V Telegramu napiš @BotFather `/newbot`, zvol jméno a uživatelské jméno bota a dostaneš jeho token. Pak
   napiš svému novému botovi libovolnou zprávu (třeba `/start`); dokud to neuděláš, bot ti psát nemůže.
2. `scripts/setup-secrets.sh telegram-token`: uloží token (skrytý vstup, ověří ho u Telegramu), vypíše chat
   id tvého chatu s botem (znovu: `scripts/setup-secrets.sh telegram-chat-id`) a restartuje `coach-mcp`.
   Prázdný `secrets/telegram_bot_token` vytvoří už `scripts/setup-secrets.sh`; bez něj se `coach-mcp` nespustí.
3. Do `.env` přidej `COACH_TELEGRAM_CHAT_ID=` s tím číslem (čas a další volby jsou v `.env.example`).
4. `docker compose up -d --build coach-mcp`

Zpráva je prostý text; první řádek je verdikt s barevným kolečkem a datem (`🟡 ŽLUTÁ · ranní briefing 7. 10.`,
s `⚠️`, když jsou nová upozornění). Briefing vypneš smazáním `COACH_TELEGRAM_CHAT_ID` z `.env` a příkazem
`docker compose up -d coach-mcp`.

Briefing chodí v `COACH_BRIEFING_TIME` (výchozí 06:30, časové pásmo `TZ`). Když hodinky ještě nemají
spánek nebo HRV, zkouší to každých `COACH_BRIEFING_RETRY_MINUTES` minut (15) a nejpozději
v `COACH_BRIEFING_CUTOFF` (09:30) pošle, co má, a napíše, co chybí. Když Garmin není přihlášený, pošle
briefing hned. Po startu služby až po cutoff se ten den nic neposílá. Ručně (třeba pro test; stav
upozornění se tím nemění):

```bash
docker compose exec coach-mcp python -m coach_mcp send-briefing
```

V logu plánovače (`docker compose logs coach-mcp`) jsou jen výsledky (`briefing status=sent|waiting|skipped|
gave_up`, `push status=…`, `garmin status=…`), nikdy obsah zprávy, čísla, chat id ani token. Ruční `send-briefing`
běží jako samostatný proces: stejné řádky vypíše jen do terminálu, v logu kontejneru nejsou.

**Návrat na verzi s ntfy** (před PR 08): starší `coach-mcp` se bez souboru `secrets/ntfy_token` nespustí
(stačí prázdný) a briefing zapíná přes `COACH_NTFY_URL` a `COACH_NTFY_TOPIC` v `.env`. Po
`git checkout <commit před PR 08>` spusť `scripts/setup-secrets.sh` (ta verze prázdný soubor vytvoří), vrať
obě proměnné do `.env` a nakonec `docker compose up -d --build --no-deps coach-mcp`. Databáze se nemění.

### Obnova Garmin tokenů

Server si access token obnovuje sám a jednou za zhruba 6 hodin udělá levné volání, aby se rotoval i refresh
token. Nové přihlášení je potřeba, když Garmin tokeny zneplatní (dlouhá nečinnost, změna hesla, změna
přihlašování na straně Garminu). Poznáš to z `healthcheck.sh`, nebo nástroje Garminu vrátí chybu
„spusť scripts/garmin-login.sh“.

```bash
scripts/garmin-login.sh            # nové přihlášení s MFA
```

Když Garmin blokuje přihlášení ze serveru (HTTP 429), přihlas se na Macu a přenes jen soubor s tokeny.
Knihovna potřebuje Python ≥ 3.12 (systémový macOS má 3.9), proto přes [uv](https://docs.astral.sh/uv/),
který si Python stáhne sám. Na Macu, po jednom řádku (zsh bere `#` jako komentář až po
`setopt interactivecomments`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
scp <vps>:fitness-coach-mcp/garmin-mcp/src/garmin_coach_mcp/login.py /tmp/garmin_login.py
uv run --python 3.13 --with garminconnect==0.3.17 python /tmp/garmin_login.py --tokens-dir ~/garmin-tokens
scp ~/garmin-tokens/garmin_tokens.json <vps>:/tmp/garmin_tokens.json
rm -rf ~/garmin-tokens /tmp/garmin_login.py
```

Na VPS:

```bash
scripts/garmin-login.sh --import /tmp/garmin_tokens.json && shred -u /tmp/garmin_tokens.json
```

Mobilní varianty loginu Garmin často odmítá s 429; knihovna pak sama zkusí webový widget, který obvykle
projde. Když 429 vrátí všechny varianty, počkej aspoň hodinu (další pokusy blokaci prodlužují) a zkus to
jednou, ideálně přes mobilní hotspot.

Když se Garmin login rozbije pro všechny (změna na straně Garminu), sleduj
[python-garminconnect](https://github.com/cyberjunky/python-garminconnect/issues). Oprava obvykle znamená
bump verze v `garmin-mcp/pyproject.toml` → `scripts/lock-deps.sh` → rebuild → `garmin-login.sh`.

### Aktualizace

Nic se neaktualizuje samo. Postup:

1. `scripts/check-updates.sh`: co je nového.
2. Uprav pin (`tailscale/Dockerfile`, `hevy-mcp/Dockerfile` včetně digestu, nebo `pyproject.toml`) a
   pro Python spusť `scripts/lock-deps.sh` (případně `--upgrade`).
3. `scripts/e2e-test.sh` (build + testy + celý OAuth/MCP flow bez dopadu na provoz).
4. `scripts/setup-secrets.sh`: doplní chybějící secrets nové verze (třeba prázdný `secrets/telegram_bot_token`); bez
   nich se služba, která je čte, nespustí. U kompletní instalace nic nemění.
5. `docker compose up -d --build` → `scripts/smoke-test.sh` → commit.

Digest pro nový tag: `docker buildx imagetools inspect <image:tag>` (řádek `Digest`).

### Zálohy

Stav je ve volumes `fitness-coach-mcp_gateway-data` (granty, audit), `fitness-coach-mcp_garmin-tokens`,
`fitness-coach-mcp_coach-data` a `fitness-coach-mcp_ts-state` + adresář `secrets/`. Při ztrátě stačí
znovu `setup-secrets.sh`, `garmin-login.sh` a znovu připojit konektory. Uzel Tailscale je pak potřeba
znovu přihlásit novým auth key.

Výjimka je `fitness-coach-mcp_coach-data`: je to **jediná kopie** cílů, omezení, plánu a poznámek kouče a
žádný skript ji nedokáže znovu vytvořit. Záloha je konzistentní, když se `coach-mcp` na chvíli zastaví
(databáze je jediný soubor; SQLite používá klasický rollback žurnál, ne WAL):

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach:ro --entrypoint tar fitness-coach-mcp/coach-mcp:0.1.0 -C /data/coach -czf - . > coach-data.tgz
docker compose start coach-mcp
```

Obnova **nahradí celý obsah** volume archivem `coach-data.tgz`; služba přitom musí stát. První `docker run`
nejdřív uloží současný stav do nového souboru `coach-data-pred-obnovou-<datum-čas>.tgz` (jiné jméno než
obnovovaný archiv, takže ho nepřepíše). Druhý `docker run` archiv celý rozbalí do dočasného adresáře a ověří,
že obsahuje neprázdný `coach.db`; teprve potom smaže obsah volume (včetně případného zbytku žurnálu
`coach.db-journal` po pádu služby, který by SQLite jinak vrátil do obnoveného souboru) a nahradí ho.
Poškozený nebo neúplný archiv skončí chybou a volume zůstane beze změny. Když některý příkaz skončí chybou,
nepokračuj dalším a služba se po `docker compose start coach-mcp` spustí se stávajícími daty. Na novém
hostu nejdřív spusť stack podle Instalace (`docker compose up -d --build`), aby volume vytvořil Compose, a
teprve potom obnovuj.

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach:ro --entrypoint tar fitness-coach-mcp/coach-mcp:0.1.0 -C /data/coach -czf - . > "coach-data-pred-obnovou-$(date +%Y%m%d-%H%M%S).tgz"
docker run --rm -i --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint sh fitness-coach-mcp/coach-mcp:0.1.0 -c 'mkdir /tmp/r && tar -C /tmp/r -xzf - && test -s /tmp/r/coach.db && find /data/coach -mindepth 1 -delete && cp -a /tmp/r/. /data/coach/' < coach-data.tgz
docker compose start coach-mcp
```

Archivy `coach-data.tgz` i `coach-data-pred-obnovou-*.tgz` obsahují osobní tréninková data: ulož je mimo repo
a mimo veřejná úložiště.

## Troubleshooting

| Příznak | Příčina / řešení |
|---|---|
| claude.ai: „Couldn't connect“ hned po přidání | Funnel neběží nebo chybí `nodeAttrs` funnel pro tag → `healthcheck.sh`. Veřejné DNS: `curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=fitness-mcp.<tailnet>&type=A'`. |
| Přihlašovací stránka hlásí „Přístup odepřen“ | Otevřel ses přes internet, ne přes tailnet. Zapni Tailscale, vypni iCloud Private Relay / vlastní DNS-over-HTTPS v prohlížeči (musí fungovat MagicDNS) a zkus znovu. |
| „Neplatné heslo nebo kód“ | Zkontroluj čas v telefonu. Každý TOTP kód jde použít jen jednou. Po 5 chybách se login zamkne (1 min, pak exponenciálně až 1 h). |
| „Žádost vypršela“ | Login stránka platí 10 min. Spusť připojení znovu z Claude. |
| Konektor po čase přestane fungovat a chce login | Refresh token vypršel nebo grant byl zrušen (`scripts/admin.sh grants list --all` ukáže důvod, např. `refresh token reuse`). Připoj znovu z tailnetu. |
| MCP volání vrací 403 | Token přišel přes Funnel z IP mimo `160.79.104.0/21`. U claude.ai to znamená, že Anthropic změnil rozsah: ověř [docs](https://platform.claude.com/docs/en/api/ip-addresses) a uprav `FUNNEL_TOKEN_CIDRS` v `.env`. |
| MCP volání vrací 502 `backend_misconfigured` | Nesedí interní token gateway ↔ backend → `scripts/setup-secrets.sh backend-tokens`. |
| Gateway se po aktualizaci nespustí: `cannot read secret /run/secrets/coach_backend_token` | `scripts/setup-secrets.sh` (doplní chybějící interní token) a pak `docker compose up -d --build coach-mcp gateway`. |
| `coach-mcp` unhealthy, v logu `database schema version N is newer` | Běží starší image než data: nasaď znovu novější verzi; volume `coach-data` nemaž. |
| `coach-mcp` se nespustí: `bind source path does not exist: …/secrets/telegram_bot_token` | Chybí soubor pro token bota (stačí prázdný) → `scripts/setup-secrets.sh` a `docker compose up -d coach-mcp`. |
| `coach-mcp` se pořád restartuje, v logu `COACH_TELEGRAM_CHAT_ID is set but … does not hold a Telegram bot token` | V `.env` je chat id, ale token chybí nebo je poškozený → `scripts/setup-secrets.sh telegram-token`, nebo chat id z `.env` smaž (briefing vypnutý). |
| Ranní notifikace nepřišla | `docker compose logs coach-mcp`: `skipped` = služba startovala až po cutoff; `gave_up` = Telegram zprávu odmítl (token, chat id, bot zablokovaný nebo mu ještě nikdo nenapsal) nebo neodpovídal; žádný řádek `briefing` = briefing vypnutý (`healthcheck.sh` to vypíše). |
| Hevy nástroje vrací 401 | Neplatný Hevy klíč nebo vypršelo Hevy PRO → `scripts/setup-secrets.sh hevy-key`. |
| Garmin nástroje: „spusť scripts/garmin-login.sh“ | Tokeny chybí nebo expirovaly → `scripts/garmin-login.sh`. |
| Garmin login: 429 / 403 od Cloudflare | Blokace z datacentrové IP → přihlas se na Macu a importuj tokeny (viz výše). |
| Garmin data: 403 u všech volání | Zkus `GARMIN_API_SESSION=curl_cffi` u služby garmin-mcp (workaround python-garminconnect #444). |
| Uzel se nepřihlásí | Auth key je spotřebovaný/expirovaný nebo bez tagu → nový klíč, `scripts/setup-secrets.sh ts-authkey`, `docker compose up -d --force-recreate ts gateway`. |
| Po restartu `ts` nefunguje gateway | Gateway sdílí jeho síťový namespace: `docker compose up -d --force-recreate gateway`. |
| `permission denied ... docker.sock` | Uživatel není ve skupině docker v aktuální session: znovu se přihlas nebo `sg docker -c "<příkaz>"`. |

## Struktura repa

```
docker-compose.yml       služby, hardening, sítě, volumes, secrets
compose.test.yml         overlay pro e2e test (bez Tailscale loginu a internetu)
.env.example             nesecretní konfigurace (hostname, owner, UID/GID, ranní briefing)
tailscale/               sidecar image (non-root) + serve.json s Funnelem
gateway/                 OAuth 2.1 gateway (Python, Starlette), testy, config.toml
hevy-mcp/                wrapper nad oficiální image (secrets ze souborů, telemetrie vypnutá)
garmin-mcp/              Garmin MCP server (Python, python-garminconnect + MCP SDK), testy
coach-mcp/               paměť kouče (Python, MCP SDK, SQLite), testy
scripts/                 setup-secrets, garmin-login, healthcheck, smoke-test, e2e-test, admin, check-updates, lock-deps
docs/coach-instructions.md   Custom Instructions pro Claude projekt „Fitness kouč“
```
