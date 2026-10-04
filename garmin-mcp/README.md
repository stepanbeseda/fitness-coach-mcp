# garmin-mcp (garmin-coach-mcp)

Tenký MCP server nad [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) 0.3.17
s ~21 vybranými nástroji pro kouče. Běží jako Streamable HTTP (stateless, JSON) na `:8000/mcp`
v privátní Docker síti; ven se dostane jen přes gateway, která posílá interní bearer token
(`/run/secrets/garmin_backend_token`). Bez tokenu server všechny MCP požadavky odmítne (fail closed).

Proč vlastní server: `AbhimanyuV04/garmin-mcp` nemá licenci ani MFA a používá login, který Garmin
v březnu 2026 rozbil; `Taxuspt/garmin_mcp` má 153 nástrojů a je zaseknutý na garminconnect 0.3.2
(bez oprav MFA a obnovy tokenů).

## Nástroje

| Čtení (readOnly) | Zápis |
|---|---|
| `get_recovery_snapshot` – readiness, HRV, spánek, Body Battery, klidová TF, stres, training status jedním voláním | `create_workout` – strukturovaný běh/kolo (+ volitelně naplánovat) |
| `get_training_readiness`, `get_hrv_trend`, `get_sleep`, `get_body_battery`, `get_daily_summary`, `get_training_status` | `schedule_workout` – na datum (idempotentní) |
| `list_activities`, `get_activity` (kola, zóny TF) | `unschedule_workout` – odebrat z kalendáře (destructive) |
| `get_performance_metrics` – předpovědi závodů (čas + tempo/km), laktátový práh (TF, tempo, výkon), tepové zóny podle sportu, výkonové zóny na kole, endurance a hill score, týdenní trend VO2max | |
| `get_personal_records` (osobní rekordy), `get_gear` (boty, kola: najeté km, počet aktivit, limit, vyřazené) | `log_weight` – ruční vážení v kg (volitelně datum, ne v budoucnu) pro sportovce bez Garmin váhy; není idempotentní |
| `get_body_composition` – vážení za období (max 366 dní, default 30): váha, BMI, % tuku, svalová hmota, % vody, kostní hmota + souhrn (první/poslední, změna, 7denní průměr) | |
| `get_nutrition` – jeden den: kalorie vs. cíl, bílkoviny/sacharidy/tuky vs. cíle, jídla, hydratace vs. cíl; co Garmin nemá, vrací `available: false` | |
| `list_workouts`, `get_workout`, `list_scheduled_workouts` | *(žádné mazání workoutů ani úpravy aktivit)* |

Cíle kroků: HR zóna 1–5, rozsah tepovky, tempo `M:SS`/km (běh), výkon W (kolo), kadence.

## Přihlášení a tokeny

Tokeny: `$GARMINTOKENS/garmin_tokens.json` (volume `garmin-tokens`, adresář 0700, soubor 0600).
Heslo se nikam neukládá. Knihovna si access token obnovuje sama (refresh token rotuje a zapisuje
se zpět); server navíc každých ~6 h udělá jedno levné volání, aby refresh token nevypršel
(`GARMIN_KEEPALIVE_HOURS`).

```bash
# první login / nový login (e-mail, heslo, MFA kód) – z kořene repa
docker compose run --rm -it --no-deps --entrypoint python garmin-mcp -m garmin_coach_mcp.login
# kontrola platnosti (exit 0 = OK)
docker compose run --rm --no-deps --entrypoint python garmin-mcp -m garmin_coach_mcp.login --check
```

(`scripts/garmin-login.sh` v kořeni repa tyto příkazy obaluje.) Běžící server si nový token soubor
načte sám při dalším volání. `GET /healthz` hlásí `garmin_auth: ok | missing | error` (bez síťového
volání; `error` = Garmin poslední session odmítl → znovu login).

### Záložní login (když Garmin/Cloudflare blokuje IP serveru)

Na Macu (domácí síť). Knihovna potřebuje Python ≥ 3.12, proto přes `uv`; příkazy vkládej po jednom:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
scp <vps>:dev/fitness-coach-mcp/garmin-mcp/src/garmin_coach_mcp/login.py /tmp/garmin_login.py
uv run --python 3.13 --with garminconnect==0.3.17 python /tmp/garmin_login.py --tokens-dir ~/garmin-tokens
scp ~/garmin-tokens/garmin_tokens.json <vps>:/tmp/garmin_tokens.json
rm -rf ~/garmin-tokens /tmp/garmin_login.py
```

Na VPS (import se zapíše s právy 0600 a ověří):

```bash
scripts/garmin-login.sh --import /tmp/garmin_tokens.json && shred -u /tmp/garmin_tokens.json
```

## Konfigurace (env)

| Proměnná | Default | Význam |
|---|---|---|
| `GARMINTOKENS` | `/data/garmin` | adresář s tokeny |
| `GARMIN_MCP_BEARER_TOKEN_FILE` | `/run/secrets/garmin_backend_token` | interní token od gateway (min. 16 znaků) |
| `TZ` | `Europe/Prague` | co znamená „dnes“ |
| `GARMIN_KEEPALIVE_HOURS` | `6` | interval keepalive (0 = vypnuto) |
| `GARMIN_API_SESSION` | `requests` | `curl_cffi` = obejití python-garminconnect #444 (HTTP 403 na datová volání) |
| `GARMIN_MCP_ALLOWED_HOSTS` | *(prázdné)* | zapne kontrolu Host hlavičky (např. `garmin-mcp:8000`) |
| `GARMIN_MCP_LOG_LEVEL` | `INFO` | logy: jen název nástroje, výsledek, doba – nikdy data ani tokeny |

## Vývoj

```bash
docker build --target test garmin-mcp            # pytest (bez sítě, fake Garmin klient)
# přegenerování zámků (hashe, linux/aarch64, CPython 3.13) po změně pyproject.toml:
docker run --rm -e HOME=/tmp -v "$PWD/garmin-mcp:/work" -w /work \
  python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b sh -c '
  pip install -q uv==0.12.21 &&
  uv pip compile pyproject.toml --generate-hashes --python-version 3.13 --python-platform aarch64-unknown-linux-gnu --no-header --annotation-style line -o requirements.lock &&
  uv pip compile pyproject.toml --extra test --generate-hashes --python-version 3.13 --python-platform aarch64-unknown-linux-gnu --no-header --annotation-style line -o requirements-dev.lock'
```

Známá nejistota: nutriční endpointy Garminu (`nutrition-service`) nejsou na reálném účtu ověřené; `get_nutrition`
hledá hodnoty pod několika názvy klíčů a při neznámém tvaru odpovědi vrací `available: false` (hydratace a vážení
jdou přes ověřené endpointy). Garmin je primární zdroj váhy; stejné vážení se nezapisuje i do Hevy.

Známá nejistota: vlastní rozsah výkonu pro kolo posíláme jako `power.zone` + `targetValueOne/Two`
(konvence typovaných modelů python-garminconnect); po prvním vytvoření takového workoutu ověř v Garmin
Connect, že se rozsah zobrazuje správně.
