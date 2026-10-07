# coach-mcp (paměť kouče)

Malý MCP server, který si pamatuje stav kouče mezi konverzacemi: cíle, omezení (zranění, dostupné dny,
vybavení), tréninkový plán po dnech se stavem a deník poznámek. Data jsou v SQLite souboru na volume
`coach-data`. Běží jako Streamable HTTP (stateless, JSON) na `:8000/mcp` v privátní Docker síti; MCP klienti
se k němu dostanou jen přes gateway (`/coach/mcp`), která posílá interní bearer token
(`/run/secrets/coach_backend_token`). Bez tokenu server všechny MCP požadavky odmítne (fail closed). Sám volá
jen `garmin-mcp` (tři čtecí nástroje, interní token `garmin_backend_token`) a volitelně Telegram Bot API
(ranní briefing).
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
| `get_daily_briefing` | čtení | dnešní ranní briefing: verdikt zelená / žlutá / červená, čísla za ním, co chybí, dnešní plán a upozornění; počítá se stejně jako ranní push, z aktuálních dat |

Všechny zápisy jsou označené jako ne-read-only a ne-destruktivní; v popisu mají větu „Only after the athlete
confirmed“, takže je kouč navrhuje a zapisuje až po potvrzení. Data jsou `YYYY-MM-DD` v místním čase
(`TZ`); „dnes“ a týden (pondělí až neděle, ISO) se počítají z něj.

## Ranní briefing

Jednou denně (a na požádání nástrojem `get_daily_briefing`) server přečte z `garmin-mcp` ranní data
(`get_recovery_snapshot`, `get_hrv_trend`, `list_scheduled_workouts`; jiné nástroje volat nemůže), přidá
dnešní plán z vlastní paměti a spočítá verdikt bez LLM. Rozhoduje **nejhorší ukazatel**; chybějící údaj
nedovolí zelenou (nejvýš žlutá) a je ve zprávě jmenovaný.

| Ukazatel | Zelená | Žlutá | Červená |
|---|---|---|---|
| Readiness (`morning_score`, jinak `score`) | 70 a víc | 40 až 69 | pod 40 |
| HRV poslední noci vs. spodní hranice baseline | aspoň na hranici | pod hranicí | pod hranicí 3 a více nocí po sobě |
| Spánek (v celých minutách) | 7 h a víc | 6 až 7 h | pod 6 h |
| Body Battery po probuzení | 50 a víc | pod 50 | nikdy |

Upozornění (zpráva je uvádí jen tehdy, když jsou **nová**):

- HRV pod baseline 3 a více nocí po sobě;
- klidová tepovka o 5 a více bpm nad 7denním průměrem;
- poměr akutní a chronické zátěže mimo optimální rozsah Garminu (stav `LOW`, `HIGH` nebo `VERY_HIGH`);
- včera naplánovaný trénink v kalendáři Garminu nebyl splněn;
- Garmin není přihlášený.

Nové = ráno platí a při minulém vyhodnocení neplatilo; stav je v tabulce `alert_state`. Výjimka je nesplněný
trénink: ten se hlásí každý den, kdy včera nějaký naplánovaný trénink nesplněn zůstal (každý den jde o jiný
trénink). Neznámá podmínka (chybějící data) stav nemění.

Plánovač běží v `COACH_BRIEFING_TIME` (výchozí 06:30, časové pásmo `TZ`). Když chybí spánek nebo HRV poslední
noci, zkouší to každých `COACH_BRIEFING_RETRY_MINUTES` minut a nejpozději v `COACH_BRIEFING_CUTOFF` pošle, co
má. Problém s přihlášením do Garminu znamená, že push jde hned. Když služba nastartuje až po cutoff, ten den
se nic neposílá. Na chybějící HRV baseline (Garmin ji počítá až po zhruba 3 týdnech nošení) se nečeká, jen se
pojmenuje. Odeslání se při dočasné chybě Telegramu (5xx, 429, síť) opakuje do cutoffu; při trvalé chybě
(jiný 4xx, třeba špatný token nebo chat id, bot zablokovaný) se ten den vzdá.

Zpráva je česky a bez formátování: první řádek je verdikt s barevným kolečkem a datem
(`🟡 ŽLUTÁ · ranní briefing 7. 10.`, s `⚠️` za kolečkem, když jsou nová upozornění), pak prázdný řádek a text.
Celá zpráva se ořízne na 4000 bajtů (Telegram bere nejvýš 4096 znaků). Bez `COACH_TELEGRAM_CHAT_ID` se plánovač
nespustí a `/healthz` hlásí `"scheduler":"off"`. Když plánovač nečekaně skončí, v logu je
`briefing status=stopped` a `/healthz` hlásí `"scheduler":"stopped"` (`healthcheck.sh` to ohlásí jako problém).
Ruční odeslání (stav upozornění se nemění):

```bash
docker compose exec coach-mcp python -m coach_mcp send-briefing
```

Návratový kód: 0 odesláno, 1 selhalo, 2 push není nastavený nebo špatný argument.

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
- `briefing_runs`: den, kdy plánovač úspěšně poslal briefing (verdikt, čas odeslání).
- `alert_state`: u každého upozornění poslední známý stav, datum vyhodnocení a kdy naposledy přišlo.

Verze schématu je v `PRAGMA user_version`, migrace jsou jen dopředné (seřazený seznam SQL skriptů, každý
v jedné transakci). Starší image odmítne otevřít novější databázi, aby ji nepoškodil. Zálohy a obnova:
hlavní `README.md` (sekce Zálohy); volume `coach-data` je jediná kopie těchto dat.

**Návrat na verzi bez ranního briefingu** (schéma 2 → 1). Starší image databázi se schématem 2 odmítne
(`database schema version 2 is newer`). Před nasazením starší verze proto smaž jen tabulky briefingu; cíle,
omezení, plán a poznámky zůstanou. Služba musí stát, příkazy spouštěj po jednom:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 2 or sys.exit(f"schema version {v}, expected 2: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE alert_state; DROP TABLE briefing_runs; PRAGMA user_version = 1; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

Druhý příkaz musí vypsat `schema version 1`. Pak nasaď starší verzi (`git checkout <starší commit>` a
`docker compose up -d --build --no-deps coach-mcp`). Když vypíše `… nothing changed`, databáze schéma 2 nemá
a nic se nezměnilo. Na prázdném volume skončí chybou `unable to open database file` a nic nevytvoří.

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
| `COACH_TELEGRAM_CHAT_ID` | *(prázdné)* | chat id tvého chatu s botem (celé číslo, skupina záporné); zapíná ranní briefing; nikdy se neloguje |
| `COACH_TELEGRAM_TOKEN_FILE` | `/run/secrets/telegram_bot_token` | token bota od @BotFather; s nastaveným chat id musí mít tvar tokenu, jinak se služba nespustí |
| `COACH_GARMIN_MCP_URL` | `http://garmin-mcp:8000/mcp` | adresa `garmin-mcp` (končí `/mcp`) |
| `COACH_GARMIN_TOKEN_FILE` | `/run/secrets/garmin_backend_token` | interní token pro `garmin-mcp` (min. 16 znaků) |
| `COACH_BRIEFING_TIME` | `06:30` | kdy plánovač začne (`HH:MM`, časové pásmo `TZ`) |
| `COACH_BRIEFING_CUTOFF` | `09:30` | nejpozdější čas odeslání, i s chybějícími daty (pozdější než čas začátku) |
| `COACH_BRIEFING_RETRY_MINUTES` | `15` | pauza mezi pokusy, když chybí spánek nebo HRV (1 až 180) |

Neplatná hodnota ukončí start s chybou (kontejner se nespustí) místo pozdějšího selhání v nástroji.

## Vývoj

```bash
docker build --target test coach-mcp
```

Testy běží při každém buildu (bez sítě, SQLite v dočasném adresáři, pevné datum 2026-10-07 a falešné hodiny
místo skutečného času; Garmin a Telegram nahrazují falešné backendy).
Zámky závislostí (`requirements.lock`, `requirements-dev.lock`) se přegenerují jen přes `scripts/lock-deps.sh`.
