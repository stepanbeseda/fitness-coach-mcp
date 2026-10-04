# coach-mcp (paměť kouče)

Malý MCP server, který si pamatuje stav kouče mezi konverzacemi: cíle, omezení (zranění, dostupné dny,
vybavení), tréninkový plán po dnech se stavem a deník poznámek. Data jsou v SQLite souboru na volume
`coach-data`. Běží jako Streamable HTTP (stateless, JSON) na `:8000/mcp` v privátní Docker síti; ven se
dostane jen přes gateway (`/coach/mcp`), která posílá interní bearer token
(`/run/secrets/coach_backend_token`). Bez tokenu server všechny MCP požadavky odmítne (fail closed).
Nic se nemaže: cíle se uzavírají, omezení se označí jako vyřešená a nahrazené tréninky zůstanou
jako `superseded`.

## Nástroje

| Nástroj | Typ | Co dělá |
|---|---|---|
| `get_coach_context` | čtení | aktivní cíle (s počtem dnů do cíle), aktivní omezení, tréninky tohoto ISO týdne a posledních 10 poznámek jedním voláním |
| `get_plan` | čtení | tréninky v rozsahu dat (výchozí tento týden, max. 92 dní) a souhrn plnění: počty planned / done / skipped / moved, zpožděné plánované tréninky, `completion_rate` |
| `set_goal` | zápis | vytvoří cíl, nebo (s `goal_id`) změní zadaná pole; uzavření stavem `achieved` / `abandoned` |
| `set_constraint` | zápis | vytvoří omezení (`kind` + název + detaily), nebo změní zadaná pole; stav `resolved`, když už neplatí |
| `save_plan` | zápis | uloží plán jednoho ISO týdne: plánované tréninky od dneška nahradí (staré zůstanou jako `superseded`), splněné, přeskočené, přesunuté a minulé nechá |
| `update_session` | zápis | nastaví stav tréninku (`planned` / `done` / `skipped` / `moved`); `moved` s `new_date` vytvoří propojenou plánovanou kopii; volitelná poznámka |
| `add_note` | zápis | přidá datovanou poznámku do deníku, volitelně k tréninku |

Všechny zápisy jsou označené jako ne-read-only a ne-destruktivní; v popisu mají větu „Only after the athlete
confirmed“, takže je kouč navrhuje a zapisuje až po potvrzení. Data jsou `YYYY-MM-DD` v místním čase
(`TZ`); „dnes“ a týden (pondělí až neděle, ISO) se počítají z něj.

## Data

- `goals`: cíl (název, volitelně `target`, `target_date`, `target_result`), stav `active` / `achieved` / `abandoned`.
- `athlete_constraints`: omezení typu `injury` / `limitation` / `availability` / `equipment` / `other`
  s názvem a volným textem, stav `active` / `resolved`.
- `sessions`: trénink s datem, typem (`running`, `cycling`, `swimming`, `strength`, `mobility`, `other`),
  popisem a volitelně intenzitou, délkou, vzdáleností a odkazem na Garmin workout nebo Hevy rutinu.
  Stavy: `planned` (naplánováno), `done` (splněno), `skipped` (přeskočeno), `moved` (přesunuto: původní
  záznam zůstane a kopie na novém datu na něj odkazuje přes `moved_from_id`), `superseded` (nahrazeno
  novějším plánem týdne; ve výchozím výpisu skryté a nepočítané).
- `notes`: datované poznámky, volitelně navázané na trénink.

Verze schématu je v `PRAGMA user_version`, migrace jsou jen dopředné (seřazený seznam SQL skriptů, každý
v jedné transakci). Starší image odmítne otevřít novější databázi, aby ji nepoškodil. Zálohy a obnova:
hlavní `README.md` (sekce Zálohy); volume `coach-data` je jediná kopie těchto dat.

## Konfigurace (env)

| Proměnná | Default | Význam |
|---|---|---|
| `COACH_MCP_DB_PATH` | `/data/coach/coach.db` | SQLite soubor (absolutní cesta, adresář musí existovat) |
| `COACH_MCP_BEARER_TOKEN_FILE` | `/run/secrets/coach_backend_token` | interní token od gateway (min. 16 znaků) |
| `COACH_MCP_HOST` | `0.0.0.0` | adresa, na které server poslouchá |
| `COACH_MCP_PORT` | `8000` | port (1 až 65535) |
| `TZ` | `Europe/Prague` | co znamená „dnes“ |
| `COACH_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` nebo `ERROR`; logy: jen název nástroje, výsledek, doba, nikdy argumenty ani uložené texty |
| `COACH_MCP_ALLOWED_HOSTS` | *(prázdné)* | zapne kontrolu Host hlavičky (např. `coach-mcp:8000`) |

Neplatná hodnota ukončí start s chybou (kontejner se nespustí) místo pozdějšího selhání v nástroji.

## Vývoj

```bash
docker build --target test coach-mcp
```

Testy běží při každém buildu (bez sítě, SQLite v dočasném adresáři, pevné datum 2026-10-07 místo hodin).
Zámky závislostí (`requirements.lock`, `requirements-dev.lock`) se přegenerují jen přes `scripts/lock-deps.sh`.
