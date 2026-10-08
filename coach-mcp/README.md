# coach-mcp (paměť kouče)

Malý MCP server, který si pamatuje stav kouče mezi konverzacemi: cíle, omezení (zranění, dostupné dny,
vybavení), tréninkový plán po dnech se stavem a deník poznámek. Data jsou v SQLite souboru na volume
`coach-data`. Běží jako Streamable HTTP (stateless, JSON) na `:8000/mcp` v privátní Docker síti; MCP klienti
se k němu dostanou jen přes gateway (`/coach/mcp`), která posílá interní bearer token
(`/run/secrets/coach_backend_token`). Bez tokenu server všechny MCP požadavky odmítne (fail closed). Sám volá
jen `garmin-mcp` (čtyři čtecí nástroje, read-only token `garmin_readonly_token`), `hevy-mcp` (dva čtecí nástroje,
interní token `hevy_backend_token`) a volitelně Telegram Bot API (ranní briefing). Každou noc si z Garminu ukládá
denní historii a z Hevy silové tréninky pro trendy, týdenní přehled a progres síly a podle historie navrhne stav
minulých plánovaných tréninků. Nic se nemaže: cíle se uzavírají, omezení se označí jako vyřešená a nahrazené
tréninky zůstanou jako `superseded`.

## Nástroje

| Nástroj | Typ | Co dělá |
|---|---|---|
| `get_coach_context` | čtení | aktivní cíle (s počtem dnů do cíle), aktivní omezení, tréninky tohoto ISO týdne, plánované tréninky posledních 7 dní s návrhem stavu (`to_confirm`) a posledních 10 poznámek jedním voláním |
| `get_plan` | čtení | tréninky v rozsahu dat (výchozí tento týden, max. 92 dní) a souhrn plnění: počty planned / done / skipped / moved, zpožděné plánované tréninky, `completion_rate`; minulé plánované tréninky mají návrh stavu `suggestion` (viz Sladění plánu) |
| `set_goal` | zápis | vytvoří cíl, nebo (s `goal_id`) změní zadaná pole; uzavření stavem `achieved` / `abandoned` |
| `set_constraint` | zápis | vytvoří omezení (`kind` + název + detaily), nebo změní zadaná pole; stav `resolved`, když už neplatí |
| `save_plan` | zápis | uloží plán jednoho ISO týdne: plánované tréninky od dneška nahradí (staré zůstanou jako `superseded`), splněné, přeskočené, přesunuté a minulé nechá |
| `update_session` | zápis | nastaví stav tréninku (`planned` / `done` / `skipped` / `moved`); `moved` s `new_date` vytvoří propojenou plánovanou kopii; volitelná poznámka |
| `add_note` | zápis | přidá datovanou poznámku do deníku, volitelně k tréninku |
| `get_daily_briefing` | čtení | dnešní ranní briefing: verdikt zelená / žlutá / červená, čísla za ním, co chybí, dnešní plán a upozornění; počítá se stejně jako ranní push, z aktuálních dat |
| `get_trends` | čtení | uložená denní historie z Garminu (HRV, spánek, klidová tepovka, Body Battery, stres, readiness, zátěž, VO2max, aktivity po sportech) za libovolné období po dnech, týdnech nebo měsících: počet, průměr, minimum, maximum, změna |
| `get_weekly_report` | čtení | jeden ISO týden proti průměru předchozích čtyř: vytrvalost po sportech, spánek, HRV, klidová tepovka, plnění plánu, silový objem a série po svalových skupinách z Hevy, nová maxima (i odhad 1RM) a varování; sekce bez dat mají `available: false` |
| `get_strength_progress` | čtení | odhad 1RM (Epley) po cvicích a trénincích z uložené historie Hevy: nejlepší série každého tréninkového dne, nejlepší, první a poslední hodnota a příznak stagnace (`plateau_weeks`, výchozí 4) |

Všechny zápisy jsou označené jako ne-read-only a ne-destruktivní; v popisu mají větu „Only after the athlete
confirmed“, takže je kouč navrhuje a zapisuje až po potvrzení. Data jsou `YYYY-MM-DD` v místním čase
(`TZ`); „dnes“ a týden (pondělí až neděle, ISO) se počítají z něj.

## Ranní briefing

Jednou denně (a na požádání nástrojem `get_daily_briefing`) server přečte z `garmin-mcp` ranní data
(`get_recovery_snapshot`, `get_hrv_trend`, `list_scheduled_workouts`), přidá
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
- včera naplánovaný trénink z plánu kouče bez aktivity stejného sportu v Garminu (podle návrhu ze sladění plánu);
- Garmin není přihlášený.

Nové = ráno platí a při minulém vyhodnocení neplatilo; stav je v tabulce `alert_state`. Výjimka jsou oba
nesplněné tréninky (kalendář Garminu i plán kouče): hlásí se každý den, kdy včera nějaký naplánovaný trénink
nesplněn zůstal (každý den jde o jiný trénink). Neznámá podmínka (chybějící data) stav nemění.

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

## Noční historie

Server si každou noc (`COACH_HISTORY_TIME`, výchozí 03:30, časové pásmo `TZ`) uloží z `garmin-mcp` denní hodnoty
do tabulky `history_days`, jeden řádek na den: readiness, HRV poslední noci a týdenní průměr, spánek (délka,
skóre), klidovou tepovku, Body Battery (nejvyšší a nejnižší), stres, akutní a chronickou zátěž, poměr zátěže,
VO2max a součty aktivit po sportech (počet, délka, vzdálenost, tréninková zátěž). Řádek dne D má spánek a HRV
noci, která skončila ráno D, a aktivity, které začaly v D. Dnešek se neukládá (není úplný): den se načte poprvé
den po něm a pak ještě dvakrát v dalších dvou dnech, aby se zachytila pozdní synchronizace hodinek. Nad uloženou
historií pracují `get_trends` a `get_weekly_report`; `get_hrv_trend` sahá jen 28 dní zpět a `get_body_battery` 31.

Doplňují se jen chybějící dny, takže po restartu se pokračuje tam, kde se skončilo (průběh je daný uloženými
daty). První běh přijde minutu po startu služby a načte `COACH_HISTORY_BACKFILL_DAYS` dní zpět (výchozí 90):
asi 470 volání Garmin Connect ve 103 voláních `garmin-mcp`, mezi nimi pauza `COACH_HISTORY_CALL_DELAY_SECONDS`
(15 s), dohromady zhruba 30 minut. Mezi `COACH_BRIEFING_TIME` a `COACH_BRIEFING_CUTOFF` běh stojí a po cutoff
pokračuje. Každou další noc je to asi 16 volání.

- Garmin odpoví 429 (omezení počtu volání): běh se zastaví a zkusí to za hodinu, pak za 2, 4 a nejvýš za 8 hodin.
- Garmin není přihlášený nebo `garmin-mcp` neodpovídá: žádné volání, další pokus za hodinu (nejpozději
  v `COACH_HISTORY_TIME`).
- Garmin Connect nefunguje (výpadek, server bez internetu): `garmin-mcp` vrací chybu u všech dnů. Po třech
  chybách po sobě se server zeptá na den, který už jednou prošel; když selže i ten, běh skončí jako
  `service_error`, výpadek dny nezapočítá a další pokus je za 1, 2, 4 a nejvýš 8 hodin.
- Chyba, která se týká jen jednoho dne (Garmin jiné dny vrací), se tomu dni započítá; den, který takhle selže
  třikrát, zůstane prázdný (`given_up` v logu). Den, který už data má, se po třech chybách přestane znovu načítat.
- Na úplně prázdné historii server nemá den na porovnání a tři chyby po sobě bere jako výpadek; vyřeší se,
  jakmile projde první den (nejpozději s dalším dnem).

Silové tréninky z Hevy: stejný běh si po Garminu přes `hevy-mcp` načte workouty změněné od posledního úplného
běhu, i upravené a smazané (`get-workout-events`, po 10 na stránku), a ke každému cviku jednou jeho šablonu kvůli
hlavní svalové skupině (`get-exercise-template`). Ukládá se jen souhrn po cvicích (pracovní série, objem v kg,
nejlepší série a její odhad 1RM podle Epleyho), ne jednotlivé série; zahřívací série se nepočítají. První běh
načte stejných `COACH_HISTORY_BACKFILL_DAYS` dní zpět se stejnou pauzou mezi voláními (jedno volání na 10
workoutů a jedno na každý cvik), další noci obvykle jedno volání. Každý výpis znovu přečte poslední 3 dny, aby
se zachytily pozdní synchronizace. Když Hevy nebo `hevy-mcp` nefunguje, Garmin část doběhne normálně, silová
data zůstanou nepokrytá (`available: false`) a další pokus je za hodinu, při omezení počtu volání za 1 až 8
hodin; nic se nevzdává, neúspěšný výpis se příště zopakuje celý a neúspěšná šablona se zkouší nejvýš jednou
denně.

Z `garmin-mcp` může server volat jen čtyři čtecí nástroje (`get_recovery_snapshot`, `get_hrv_trend`,
`list_scheduled_workouts`, `list_activities`). Jeho zapisovací nástroje kód kouče nevolá a `garmin-mcp` je
s read-only tokenem kouče odmítne dřív, než se spustí. Z `hevy-mcp` smí volat jen `get-workout-events`
a `get-exercise-template`. Log má na každý běh jeden řádek za Garmin:
`history status=… fetched=… failed=… pending=… given_up=… ms=… matched=… no_activity=…` (`reconcile=error kind=…`,
když sladění plánu selže, běh historie tím neselže) a jeden za Hevy:
`history status=… part=strength workouts=… deleted=… templates=… failed=… ms=…` (bez dat a hodnot). Při
`COACH_HISTORY_TIME` uvnitř okna briefingu je po startu navíc jeden řádek `history status=config_warning`.
`/healthz` hlásí pole `history`: `on`, `off` (`COACH_HISTORY_ENABLED=false`) nebo `stopped` (úloha nečekaně
skončila).

## Sladění plánu

Po každém běhu noční historie server porovná plánované tréninky posledních 7 dní (bez dneška) s uloženými
součty aktivit stejného dne a sportu (`running`, `cycling`, `swimming`, `strength`; `mobility` a `other` se
nepárují) a uloží návrh do tabulky `session_suggestions`: `matched` s důkazem (sport, počet aktivit, délka
a vzdálenost toho sportu za den) nebo `no_activity`. Den, jehož aktivity se ještě nenačetly nebo se po třech
chybách vzdaly, návrh nemá. Víc tréninků stejného sportu v jeden den: aktivity se nejdřív přiřadí tréninkům
už označeným `done`, zbytek plánovaným v pořadí plánu. Návrh má `final: false`, dokud se den může ještě znovu
načíst (do třetího dne po něm, pozdní synchronizace hodinek). Stav tréninku server nikdy nemění: `get_plan`
a `get_coach_context` ukazují návrh jen u tréninků ve stavu `planned`, kouč ho ověří a po potvrzení zapíše
`update_session`. Silové tréninky se zatím párují jen se silovým záznamem z hodinek (`strength_training`),
ne s Hevy. Bez noční historie (`COACH_HISTORY_ENABLED=false`) návrhy nevznikají. Ranní briefing jmenuje
včerejší plánované tréninky s `no_activity` (upozornění výše).

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
- `session_suggestions`: návrh stavu plánovaného tréninku (`matched` s důkazem nebo `no_activity`, `final`, datum
  vyhodnocení); jeden řádek na trénink, každou noc se přepíše.
- `history_days`: jeden řádek na den s hodnotami z Garminu (readiness, HRV, spánek, klidová tepovka, Body Battery,
  stres, zátěž, VO2max) a součty aktivit po sportech; kdy a s kolika chybami se den naposledy načetl.
- `strength_workouts`: jeden řádek na workout z Hevy (id, místní datum začátku, čas poslední změny v Hevy) se
  souhrnem po cvicích (šablona, název, pracovní série, objem v kg, nejlepší série a odhad 1RM); smazaný workout
  zůstane jako značka `deleted` bez cviků.
- `hevy_templates`: šablony cviků z Hevy (název, typ, hlavní svalová skupina), nebo že šablona v Hevy není.
- `hevy_sync`: odkud a do kdy je silová historie úplná (poslední úplný běh).

Verze schématu je v `PRAGMA user_version`, migrace jsou jen dopředné (seřazený seznam SQL skriptů, každý
v jedné transakci). Starší image odmítne otevřít novější databázi, aby ji nepoškodil. Zálohy za běhu
(`python -m coach_mcp backup`, volá ho `scripts/backup.sh`) a obnova (`python -m coach_mcp restore`): hlavní
`README.md` (sekce Zálohy); volume `coach-data` je jediná kopie těchto dat.

Před přechodem na starší verzi spusť `scripts/backup.sh`.

**Návrat na verzi bez sladění plánu** (schéma 4 → 3). Starší image databázi se schématem 4 odmítne
(`database schema version 4 is newer`). Před nasazením starší verze proto smaž jen tabulku návrhů; cíle,
omezení, plán, poznámky, stav briefingu i historie zůstanou. Tabulka `alert_state` si nechá rozšířenou
kontrolu klíčů (i řádek `missed_planned_session`), starší verze to ignoruje a po novém nasazení migrace 4
proběhne znovu. Služba musí stát, příkazy spouštěj po jednom:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 4 or sys.exit(f"schema version {v}, expected 4: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE session_suggestions; PRAGMA user_version = 3; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

Druhý příkaz musí vypsat `schema version 3`. Pak nasaď starší verzi (`git checkout <commit před PR 13>`
a `docker compose up -d --build --no-deps coach-mcp`). Když vypíše `… nothing changed`, databáze schéma 4
nemá a nic se nezměnilo. Na prázdném volume skončí chybou `unable to open database file` a nic nevytvoří.
Návrat na ještě starší verzi je nejdřív tento postup 4 → 3 a potom následující.

**Návrat na verzi bez silové historie** (schéma 4 → 3). Starší image databázi se schématem 4 odmítne
(`database schema version 4 is newer`). Před nasazením starší verze proto smaž jen tabulky silové historie; cíle,
omezení, plán, poznámky, stav briefingu i historie z Garminu zůstanou (silovou historii si server po novém
nasazení znovu načte z Hevy). Služba musí stát, příkazy spouštěj po jednom:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 4 or sys.exit(f"schema version {v}, expected 4: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE strength_workouts; DROP TABLE hevy_templates; DROP TABLE hevy_sync; PRAGMA user_version = 3; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

Druhý příkaz musí vypsat `schema version 3`. Pak nasaď starší verzi (`git checkout <commit před PR 09>` a
`docker compose up -d --build --no-deps coach-mcp`). Když vypíše `… nothing changed`, databáze schéma 4 nemá
a nic se nezměnilo. Na prázdném volume skončí chybou `unable to open database file` a nic nevytvoří. Návrat na
starší verze je nejdřív tento postup 4 → 3 a potom následující.

**Návrat na verzi bez historie** (schéma 3 → 2). Starší image databázi se schématem 3 odmítne
(`database schema version 3 is newer`). Před nasazením starší verze proto smaž jen tabulku historie; cíle,
omezení, plán, poznámky a stav briefingu zůstanou (historii si server po novém nasazení znovu načte z Garminu).
Služba musí stát, příkazy spouštěj po jednom:

```bash
docker compose stop coach-mcp
docker run --rm --network none -v fitness-coach-mcp_coach-data:/data/coach --entrypoint python fitness-coach-mcp/coach-mcp:0.1.0 -c 'import sqlite3, sys; c = sqlite3.connect("file:/data/coach/coach.db?mode=rw", uri=True); v = c.execute("PRAGMA user_version").fetchone()[0]; v == 3 or sys.exit(f"schema version {v}, expected 3: nothing changed"); c.executescript("BEGIN IMMEDIATE; DROP TABLE history_days; PRAGMA user_version = 2; COMMIT;"); print("schema version", c.execute("PRAGMA user_version").fetchone()[0])'
```

Druhý příkaz musí vypsat `schema version 2`. Pak nasaď starší verzi (`git checkout <commit před PR 05>` a
`docker compose up -d --build --no-deps coach-mcp`). Když vypíše `… nothing changed`, databáze schéma 3 nemá
a nic se nezměnilo. Na prázdném volume skončí chybou `unable to open database file` a nic nevytvoří. Návrat až
na verzi bez briefingu (schéma 1) je nejdřív tento postup 3 → 2 a potom následující 2 → 1.

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
| `TZ` | `Europe/Prague` | co znamená „dnes“; v compose se nastavuje přes `ATHLETE_TZ` v `.env` |
| `COACH_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` nebo `ERROR`; logy: jen název nástroje, výsledek, doba, nikdy argumenty ani uložené texty |
| `COACH_MCP_ALLOWED_HOSTS` | *(prázdné)* | zapne kontrolu Host hlavičky (např. `coach-mcp:8000`) |
| `COACH_TELEGRAM_CHAT_ID` | *(prázdné)* | chat id tvého chatu s botem (celé číslo, skupina záporné); zapíná ranní briefing; nikdy se neloguje |
| `COACH_TELEGRAM_TOKEN_FILE` | `/run/secrets/telegram_bot_token` | token bota od @BotFather; s nastaveným chat id musí mít tvar tokenu, jinak se služba nespustí |
| `COACH_GARMIN_MCP_URL` | `http://garmin-mcp:8000/mcp` | adresa `garmin-mcp` (končí `/mcp`) |
| `COACH_GARMIN_TOKEN_FILE` | `/run/secrets/garmin_readonly_token` | read-only token pro `garmin-mcp` (min. 16 znaků); zapisovací nástroje s ním `garmin-mcp` odmítne |
| `COACH_HEVY_MCP_URL` | `http://hevy-mcp:3000/mcp` | adresa `hevy-mcp` (končí `/mcp`) |
| `COACH_HEVY_TOKEN_FILE` | `/run/secrets/hevy_backend_token` | interní token pro `hevy-mcp` (min. 16 znaků); bez něj se silová historie nenačítá, Garmin část běží dál |
| `COACH_BRIEFING_TIME` | `06:30` | kdy plánovač začne (`HH:MM`, časové pásmo `TZ`) |
| `COACH_BRIEFING_CUTOFF` | `09:30` | nejpozdější čas odeslání, i s chybějícími daty (pozdější než čas začátku) |
| `COACH_BRIEFING_RETRY_MINUTES` | `15` | pauza mezi pokusy, když chybí spánek nebo HRV (1 až 180) |
| `COACH_HISTORY_ENABLED` | `true` | noční historie z Garminu a Hevy (`true` / `false`) |
| `COACH_HISTORY_TIME` | `03:30` | kdy (`HH:MM`, `TZ`); čas uvnitř okna briefingu posune běh na cutoff |
| `COACH_HISTORY_BACKFILL_DAYS` | `90` | kolik dní zpět načíst (1 až 366) |
| `COACH_HISTORY_CALL_DELAY_SECONDS` | `15` | pauza mezi voláními `garmin-mcp` a `hevy-mcp` (1 až 600 s) |

Neplatná hodnota ukončí start s chybou (kontejner se nespustí) místo pozdějšího selhání v nástroji.

## Vývoj

```bash
docker build --target test coach-mcp
```

Testy běží při každém buildu (bez sítě, SQLite v dočasném adresáři, pevné datum 2026-10-07 a falešné hodiny
místo skutečného času; Garmin a Telegram nahrazují falešné backendy).
Zámky závislostí (`requirements.lock`, `requirements-dev.lock`) se přegenerují jen přes `scripts/lock-deps.sh`.
