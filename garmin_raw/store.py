"""store.py — слой SQLite: источник правды одного профиля (§3 ТЗ).

Одна БД на профиль. Версия СХЕМЫ — через PRAGMA user_version + раннер миграций
(структура таблиц меняется независимо от версии ФОРМУЛ — §7 ТЗ). Прикладное
состояние (algo_version, диапазон данных, политика) — в key-value таблице meta.

Главный инвариант (§1 ТЗ): БД — источник правды, не ускоритель. Сырьё завершённой
тренировки неизменно → хранится навечно, без TTL; обогащение пересчитывается из
локального сырья, не из сети.

Этот файл — этап 1 (§14): схема + миграции + CRUD каталога/meta/raw. CRUD для
enriched/aggregates добавляется на этапах 4–5; таблицы создаются уже сейчас.
"""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional

# Версия СХЕМЫ БД (структура таблиц). НЕ путать с ALGO_VERSION (версия формул).
SCHEMA_VERSION = 5


# --------------------------------------------------------------------------- #
# Миграции. Каждая — (версия, SQL). Применяются по возрастанию для всех версий
# больше текущего PRAGMA user_version. Никогда не переписывать прошлые миграции —
# только добавлять новые (иначе чужие БД не мигрируют корректно).
# --------------------------------------------------------------------------- #
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- §3.1 каталог: дёшево, для ВСЕХ тренировок, никогда не выкидывается.
        -- Колонки делятся по роли (см. ТЗ §3.1, финальная схема):
        --  НАДЁЖНЫЕ, фильтруемые: max_hr, distance_m, duration_s, moving_time_s,
        --                         avg_cadence, флаги достоверности
        --  ОБМАНЧИВЫЕ (_raw): avg_hr_raw, avg_speed_raw — среднее разрушено
        --                     диапазоном, НЕ фильтруемые, суффикс = «не верь никогда»
        --  ФЛАГ НАЛИЧИЯ ДАТЧИКА: avg_gct, avg_vert_ratio — не аналитика
        --  ЧУЖАЯ ПРОИЗВОДНАЯ: garmin_training_load_derived — не фильтр (§1 запрет)
        --  ИСТОЧНИК ИСТИНЫ: summary_json — всё, что не разложено в колонки
        CREATE TABLE IF NOT EXISTS activities (
            activity_id     INTEGER PRIMARY KEY,
            date            TEXT,        -- YYYY-MM-DD (с годом)
            start_time      TEXT,        -- ISO/UTC
            sport           TEXT,
            duration_s      REAL,        -- надёжное, фильтруемое
            distance_m      REAL,        -- надёжное, фильтруемое
            moving_time_s   REAL,        -- NULL до обогащения; надёжное, фильтруемое
            lap_count       INTEGER,
            max_hr          INTEGER,     -- надёжное, фильтруемое
            avg_cadence     REAL,        -- фильтруемое БЕЗ _raw (узкий диапазон; край strides снимается median_crossings/lap_count)
            avg_hr_raw      REAL,        -- НЕ фильтруемое: среднее разрушено диапазоном на интервалах (§5.1)
            avg_speed_raw   REAL,        -- НЕ фильтруемое: то же
            avg_gct         REAL,        -- флаг наличия датчика, не аналитика (привязка к темпу — на обогащении)
            avg_vert_ratio  REAL,        -- флаг наличия датчика
            garmin_training_load_derived REAL,  -- чужая производная Garmin; НЕ фильтр (§1)
            has_biomech_sensor INTEGER,  -- флаг достоверности §3.2; NULL до уточнения
            gps_validated      INTEGER,  -- флаг достоверности §3.2; NULL до уточнения
            device_model    TEXT,        -- заполняется классификацией позже
            hr_source       TEXT,        -- chest/optical/unknown
            gps_type        TEXT,        -- outdoor/treadmill/track/none
            biomech_source  TEXT,        -- run-pod/foot-pod/watch-only
            summary_json    TEXT         -- сырая сводка (strip_pii), источник истины
        );
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
        CREATE INDEX IF NOT EXISTS idx_activities_distance ON activities(distance_m);
        CREATE INDEX IF NOT EXISTS idx_activities_maxhr ON activities(max_hr);

        -- §3.2 сырьё: навечно, без TTL. Прошлое неизменно.
        CREATE TABLE IF NOT EXISTS activity_raw (
            activity_id  INTEGER NOT NULL,
            kind         TEXT NOT NULL,      -- laps/streams/comment
            payload      BLOB NOT NULL,      -- zlib(JSON utf-8)
            fetched_at   TEXT NOT NULL,
            PRIMARY KEY (activity_id, kind)
        );

        -- §3.3 индекс интересности: дёшево, для ВСЕХ; пере-выбор A-множества без перекачки
        CREATE TABLE IF NOT EXISTS interest_index (
            activity_id       INTEGER PRIMARY KEY,
            variability       REAL,
            median_crossings  INTEGER,
            hr_above_easy_s   REAL,
            interest_score    REAL,
            algo_version      TEXT
        );

        -- §3.4 обогащение per-activity: версионировано (ключ включает algo_version)
        CREATE TABLE IF NOT EXISTS activity_enriched (
            activity_id     INTEGER NOT NULL,
            algo_version    TEXT NOT NULL,
            hr_histogram    TEXT,
            pace_histogram  TEXT,
            clusters        TEXT,
            pace_variance   REAL,
            hr_variance     REAL,
            biomech_by_pace TEXT,
            lactate_marks   TEXT,
            elevation       TEXT,
            confidence      TEXT,
            computed_at     TEXT,
            PRIMARY KEY (activity_id, algo_version)
        );

        -- §3.5 кросс-активностные агрегаты: считает КОННЕКТОР, не LLM; версионировано
        CREATE TABLE IF NOT EXISTS period_aggregates (
            period_key             TEXT NOT NULL,   -- напр. 2026-Q2
            algo_version           TEXT NOT NULL,
            pace_at_fixed_hr       TEXT,
            gct_at_fixed_pace      TEXT,
            decoupling             TEXT,
            hr_recovery            TEXT,
            intensity_distribution TEXT,
            volume_7d              TEXT,
            volume_28d             TEXT,
            max_hr_accumulated     INTEGER,
            computed_at            TEXT,
            PRIMARY KEY (period_key, algo_version)
        );

        -- §3.6 прикладное состояние БД (key-value)
        CREATE TABLE IF NOT EXISTS meta (
            key    TEXT PRIMARY KEY,
            value  TEXT
        );
        """,
    ),
    (
        2,
        """
        -- ─────────────────────────────────────────────────────────────────────
        -- Миграция v2 (этап 5). Две независимые правки, обе безопасны для сырья:
        --   (1) period_aggregates: первая редакция (v1) была СТАРОЙ схемой с
        --       зашитыми якорями (pace_at_fixed_hr, gct_at_fixed_pace) и именами
        --       (intensity_distribution) — ТЗ §13 запрещает их коннектору.
        --       Таблица ПУСТА (этап 5 не делался, CRUD записи не было) → DROP+CREATE
        --       без переноса данных безопасен. Новая схема — §3.5 ТЗ:
        --       якорь-нейтральные сетки by_source + provenance, без имён/зон.
        --   (2) activity_enriched: ALTER ADD 2 колонки под бакетные выжимки
        --       enrich-0.3.0. ALTER ADD COLUMN в SQLite — метаданные-операция, НЕ
        --       переписывает таблицу, НЕ трогает activity_raw. Старые 0.2.2 строки
        --       получают NULL в новых полях и остаются ВАЛИДНЫМИ для чтения; новые
        --       0.3.0 строки пишутся рядом под своим ключом (recompute, resumable).
        -- Ни один оператор не касается activity_raw/activities/interest_index/meta.
        -- ─────────────────────────────────────────────────────────────────────

        DROP TABLE IF EXISTS period_aggregates;
        CREATE TABLE period_aggregates (
            period_key          TEXT NOT NULL,   -- напр. 2026-Q2 (квартальные срезы, МЕТОД §5.4)
            algo_version        TEXT NOT NULL,
            -- якорь-нейтральные, без имён (§3.5):
            volume_7d           TEXT,    -- скользящее окно, км (якоря нет)
            volume_28d          TEXT,
            max_hr_accumulated  INTEGER, -- ~97-й перцентиль распределения per-activity max, НЕ max() (§2.4)
            decoupling          TEXT,    -- механический ratio (2-я пол./1-я), без имени «база держит» (LLM)
            hr_recovery         TEXT,    -- падение HR после кругов быстрее медианы темпа, без имени (LLM)
            pace_by_hr_grid     TEXT,    -- темп по сетке HR, by_source {chest:{...},optical:{...}} (§3.5.1)
            gct_by_pace_grid    TEXT,    -- GCT/vert-ratio по сетке темпа, одной строкой (биомеханика железо-незав.)
            provenance          TEXT,    -- ФАКТ происхождения: hr_sources/device_models с долями и n; без суждения
            computed_at         TEXT,
            PRIMARY KEY (period_key, algo_version)
        );

        -- бакетные выжимки enrich-0.3.0 (per-row версия в PK уже различает 0.2.2/0.3.0)
        ALTER TABLE activity_enriched ADD COLUMN biomech_by_pace_bucket TEXT;
        ALTER TABLE activity_enriched ADD COLUMN pace_by_hr_bucket TEXT;
        """,
    ),
    (
        3,
        """
        -- Миграция v3 (этап 5, enrich-0.5.0). Две колонки per-activity под
        -- decoupling и hr_recovery — streams/laps-зависимые поля для aggregate.
        -- ALTER ADD COLUMN: метаданные-операция, НЕ переписывает таблицу, НЕ трогает
        -- activity_raw. Старые версии строк (0.2.2…0.4.0) получают NULL и остаются
        -- валидными; новые 0.5.0 пишутся рядом под своим ключом (recompute resumable).
        -- decoupling: ratio (2-я пол./1-я) на ровном темпе; hr_recovery: падение HR
        -- после рабочих кругов. Оба БЕЗ имён/якорей (§3.5) — суждение LLM.
        ALTER TABLE activity_enriched ADD COLUMN decoupling TEXT;
        ALTER TABLE activity_enriched ADD COLUMN hr_recovery TEXT;
        """,
    ),
    (
        4,
        """
        -- ─────────────────────────────────────────────────────────────────────
        -- Миграция v4 (этап 7). Рукотворные данные через MCP: лактатные замеры и
        -- заметки, внесённые LLM/человеком в разговоре. Источник был в разговоре →
        -- НЕ воспроизводимо из сырья → физически изолировано от recompute (ТЗ §3.6).
        --
        -- Две таблицы, разведённые по инварианту «намерение vs раствор»:
        --   user_data              — НАМЕРЕНИЕ (mmol, at_time, user_ref). Вечно,
        --                            read-only для recompute (как activity_raw).
        --                            mark_id рождается ЗДЕСЬ при записи, навечно.
        --   user_lactate_resolved  — РАСТВОР (lap, hr_at, pace_at). Версионируемо,
        --                            recompute пересчитывает из user_data+streams
        --                            (DELETE версии + INSERT). mark_id — FK, НЕ
        --                            порождается здесь: иначе идентичность метки
        --                            завязалась бы на момент резолва и recompute
        --                            «вспомнил бы про user_data» через PK — протечка
        --                            изоляции. Идентичность живёт в намерении.
        --
        -- Обе — НОВЫЕ таблицы (CREATE), НЕ трогают activity_raw/activities/
        -- activity_enriched/period_aggregates. Существующие данные не затрагиваются.
        --
        -- kind разложен в типизированные колонки (mmol/user_ref/at_time/note_text),
        -- а не в opaque payload: at_time/user_ref нужны резолверу как первоклассные,
        -- mmol — читаемое/фильтруемое число (философия проекта: явные поля > JSON,
        -- где поле несёт смысл).
        --
        -- mark_id — INTEGER PRIMARY KEY AUTOINCREMENT: монотонный, НЕ переиспользуется
        -- после удаления (важно для вечной идентичности — удалённый id не должен
        -- позже достаться другой метке).
        --
        -- at_time — wall-clock UTC (мс epoch), НЕ elapsed-moving: замер лактата часто
        -- в ПАУЗЕ после рабочего куска (человек остановился, уколол палец). elapsed-
        -- moving выкинул бы точку паузы (та же логика, что hr_recovery Q8: края по
        -- wall-clock, moving-маска убрала бы измеряемую точку). Согласовано с потоком
        -- (directTimestamp — wall-clock мс) и lap bounds (startTimeGMT UTC).
        -- ─────────────────────────────────────────────────────────────────────

        CREATE TABLE IF NOT EXISTS user_data (
            mark_id     INTEGER PRIMARY KEY AUTOINCREMENT,  -- вечная идентичность метки
            activity_id INTEGER NOT NULL,
            kind        TEXT NOT NULL,      -- 'lactate' | 'note'
            mmol        REAL,               -- kind=lactate; NULL для note
            user_ref    TEXT,               -- напр. 'lap4'; NULL если задан at_time
            at_time     INTEGER,            -- wall-clock UTC мс; NULL если только user_ref
            note_text   TEXT,               -- kind=note; NULL для lactate
            source      TEXT NOT NULL,      -- 'llm' | 'manual'
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_data_activity ON user_data(activity_id);

        CREATE TABLE IF NOT EXISTS user_lactate_resolved (
            mark_id      INTEGER NOT NULL,  -- FK → user_data.mark_id (НЕ порождается здесь)
            algo_version TEXT NOT NULL,
            lap          INTEGER,           -- круг, в который попала секунда замера
            hr_at        REAL,              -- пульс ближайшей секунды потока (калибр. точка)
            pace_at      REAL,              -- темп той же секунды
            computed_at  TEXT,
            PRIMARY KEY (mark_id, algo_version),
            FOREIGN KEY (mark_id) REFERENCES user_data(mark_id) ON DELETE CASCADE
        );
        """,
    ),
    (
        5,
        """
        -- ─────────────────────────────────────────────────────────────────────
        -- Миграция v5 (этап 7). Расщепление pending на два состояния по ПРОИСХОЖДЕНИЮ
        -- + доказательство вердикта. Аддитивно поверх v4 (только ADD COLUMN с DEFAULT —
        -- существующие строки получают validation='ok', ничего не переписывается).
        --
        -- ЗАЧЕМ. pending был вычисляемым на чтении (нет resolved-строки под версией) —
        -- один цвет, причину не различить. Но «метку добавили, ждём streams» и «метку
        -- добавили user_ref='lapN', а laps ещё нет → круг N недоказуем» — РАЗНЫЕ pending:
        --   pending_resolve    — вход провалидирован (at_time; или user_ref+laps есть+
        --                        круг N есть), ждём streams. recompute → резолвить.
        --   pending_validation — вход НЕ провалидирован (user_ref='lapN', laps нет →
        --                        N недоказуем). recompute после дозакачки laps → сначала
        --                        провалидировать N, и если круга нет — ошибка (invalid).
        -- Без различения опечатка 'lap40' на тренировке без laps тонет: входная
        -- валидация её не поймала (laps не было), одноцветный pending не заставит
        -- recompute проверить N при появлении laps → метка тихо None или вечный pending.
        -- Симметрия с hr_recovery: no_laps (нет в кэше, чинится) vs no_fast_laps
        -- (честная неопределимость) — тот же водораздел «доказуема ли невозможность
        -- на имеющейся структуре», применённый к валидации входа.
        --
        -- validation — ПРОИЗВОДНОЕ от laps (не от резолвера!): «есть ли круг N» не
        -- зависит от algo_version → колонка БЕЗ версии, пересчитывается при смене laps,
        -- один раз на метку (не на метку×версию). Раствор ×algo_version (от streams+
        -- резолвера), validation ×laps-без-версии. Производное следует за своим
        -- источником, не за глобальным счётчиком.
        --   'ok'         — вход провалидирован (at_time; или user_ref+круг N в laps есть)
        --   'deferred'   — валидация отложена (user_ref='lapN', laps нет → N недоказуем)
        --   'invalid'    — отложенная валидация провалена (laps есть, круга N нет).
        --                  НЕ терминальна: validation=f(текущие laps) → при смене laps
        --                  может ожить в 'ok'. invalid — производное, как раствор; ухода
        --                  в invalid снимает раствор той метки (recompute-инвариант,
        --                  реализуется в sync 8a: validation ПЕРЕД резолвом).
        --
        -- lap_count — ДОКАЗАТЕЛЬСТВО вердикта рядом с вердиктом: тот же f(laps), тот же
        -- момент, та же беcверсионность. Нужен, чтобы invalid был самодостаточен на
        -- чтении («круга N нет, в тренировке M кругов» — человек видит опечатку без
        -- второго обращения), НЕ дочитывая laps из raw на compact (то касание raw
        -- выпалывал B2). NULL = «неприменимо» (at_time-метка/заметка — кругового вопроса
        -- нет) ИЛИ «ещё не вычислен» (deferred до laps). Читается ТОЛЬКО при
        -- validation='invalid', где заведомо заполнен → NULL нигде не двусмыслен.
        --
        -- store ТУПОЙ: validation/lap_count он ХРАНИТ, но НЕ вычисляет и НЕ судит.
        -- Вычисляет тот, у кого laps на руках: тул при немедленном резолве (add_lactate),
        -- sync-ветка при recompute (батчем). id-чек «activity_id в каталоге» — тоже в
        -- туле (8a), не здесь: store пишет что дали.
        -- ─────────────────────────────────────────────────────────────────────

        ALTER TABLE user_data ADD COLUMN validation TEXT NOT NULL DEFAULT 'ok';
        ALTER TABLE user_data ADD COLUMN lap_count INTEGER;
        """,
    ),
]


def _compress(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _decompress(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Store:
    """Соединение с БД профиля + операции. Контекстный менеджер.

    Использование:
        with Store(profile.db_path) as st:
            st.upsert_activities(rows)
            start, end = st.activity_date_range()
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")       # конкурентное чтение при sync
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._migrate()

    # ------------------------------------------------------------------ #
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Миграции
    # ------------------------------------------------------------------ #
    def _migrate(self) -> None:
        cur = self.conn.execute("PRAGMA user_version;")
        version = cur.fetchone()[0]
        for target, sql in _MIGRATIONS:
            if target > version:
                self.conn.executescript(sql)
                self.conn.execute(f"PRAGMA user_version={target};")
                self.conn.commit()
                version = target

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version;").fetchone()[0]

    # ------------------------------------------------------------------ #
    # meta (key-value)
    # ------------------------------------------------------------------ #
    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Каталог activities
    # ------------------------------------------------------------------ #
    _ACT_COLS = (
        "activity_id", "date", "start_time", "sport", "duration_s", "distance_m",
        "moving_time_s", "lap_count", "max_hr", "avg_cadence", "avg_hr_raw",
        "avg_speed_raw", "avg_gct", "avg_vert_ratio", "garmin_training_load_derived",
        "has_biomech_sensor", "gps_validated", "device_model", "hr_source",
        "gps_type", "biomech_source", "summary_json",
    )

    def upsert_activities(self, rows: Iterable[dict]) -> int:
        """Вставка/обновление каталожных строк. Не затирает поля, которых нет в row,
        кроме явно переданных. Возвращает число обработанных строк."""
        cols = self._ACT_COLS
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "activity_id")
        sql = (
            f"INSERT INTO activities ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {updates}"
        )
        data = [tuple(r.get(c) for c in cols) for r in rows]
        self.conn.executemany(sql, data)
        self.conn.commit()
        return len(data)

    def activity_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """min/max date по каталогу — это и есть garmin_range (§12.1, побочный продукт)."""
        row = self.conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM activities"
        ).fetchone()
        return (row["lo"], row["hi"])

    def activity_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    def has_raw(self, activity_id: int, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activity_raw WHERE activity_id=? AND kind=?",
            (activity_id, kind),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ #
    # Обогащение activity_enriched (версионировано по algo_version)
    # ------------------------------------------------------------------ #
    _ENR_COLS = (
        "activity_id", "algo_version", "hr_histogram", "pace_histogram", "clusters",
        "pace_variance", "hr_variance", "biomech_by_pace", "lactate_marks",
        "elevation", "confidence", "computed_at",
        "biomech_by_pace_bucket", "pace_by_hr_bucket",
        "decoupling", "hr_recovery",
    )

    def has_enriched(self, activity_id: int, algo_version: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM activity_enriched WHERE activity_id=? AND algo_version=?",
            (activity_id, algo_version),
        ).fetchone()
        return row is not None

    def put_enriched(self, activity_id: int, enriched: dict) -> None:
        """Пишет результат enrich_activity. JSON-поля сериализуются. Идемпотентно
        по (activity_id, algo_version) — пересчёт перезаписывает.

        Дополнительно синхронизирует moving_time_s и median_crossings в каталог
        (там они были NULL до обогащения)."""
        av = enriched.get("algo_version")
        row = {
            "activity_id": activity_id,
            "algo_version": av,
            "hr_histogram": json.dumps(enriched.get("hr_histogram"), ensure_ascii=False),
            "pace_histogram": json.dumps(enriched.get("pace_histogram"), ensure_ascii=False),
            "clusters": json.dumps(enriched.get("clusters"), ensure_ascii=False),
            "pace_variance": enriched.get("pace_variance"),
            "hr_variance": enriched.get("hr_variance"),
            "biomech_by_pace": json.dumps(enriched.get("biomech_by_pace"), ensure_ascii=False),
            "lactate_marks": json.dumps(enriched.get("lactate_marks"), ensure_ascii=False),
            "elevation": json.dumps(enriched.get("elevation"), ensure_ascii=False),
            "confidence": json.dumps(enriched.get("confidence"), ensure_ascii=False),
            "computed_at": _iso_now(),
            "biomech_by_pace_bucket": json.dumps(enriched.get("biomech_by_pace_bucket"), ensure_ascii=False),
            "pace_by_hr_bucket": json.dumps(enriched.get("pace_by_hr_bucket"), ensure_ascii=False),
            "decoupling": json.dumps(enriched.get("decoupling"), ensure_ascii=False),
            "hr_recovery": json.dumps(enriched.get("hr_recovery"), ensure_ascii=False),
        }
        cols = self._ENR_COLS
        ph = ",".join("?" for _ in cols)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("activity_id", "algo_version"))
        self.conn.execute(
            f"INSERT INTO activity_enriched ({','.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(activity_id,algo_version) DO UPDATE SET {upd}",
            tuple(row[c] for c in cols),
        )
        # каталог: проставляем посчитанные обогащением поля
        mt = enriched.get("moving_time_s")
        mc = enriched.get("median_crossings")
        mx = enriched.get("max_hr")
        hrs = enriched.get("hr_source")   # §3.5.1: chest (balance в потоке) / unknown
        # hr_source пишем ПРЯМО (не COALESCE): при recompute со сменой логики извлечения
        # он должен ОБНОВИТЬСЯ, а COALESCE сохранил бы старое значение. max_hr —
        # COALESCE, т.к. может быть из сводки до обогащения; hr_source только из enrich.
        self.conn.execute(
            "UPDATE activities SET moving_time_s=COALESCE(?,moving_time_s), "
            "max_hr=COALESCE(?,max_hr), hr_source=? WHERE activity_id=?",
            (mt, mx, hrs, activity_id),
        )
        # median_crossings храним в interest_index (дешёвый сигнал для отбора/фильтра)
        self.conn.execute(
            "INSERT INTO interest_index(activity_id,median_crossings,algo_version) "
            "VALUES(?,?,?) ON CONFLICT(activity_id) DO UPDATE SET "
            "median_crossings=excluded.median_crossings, algo_version=excluded.algo_version",
            (activity_id, mc, av),
        )
        self.conn.commit()

    def get_enriched(self, activity_id: int, algo_version: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM activity_enriched WHERE activity_id=? AND algo_version=?",
            (activity_id, algo_version),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for k in ("hr_histogram", "pace_histogram", "clusters", "biomech_by_pace",
                  "lactate_marks", "elevation", "confidence",
                  "biomech_by_pace_bucket", "pace_by_hr_bucket",
                  "decoupling", "hr_recovery"):
            if out.get(k):
                out[k] = json.loads(out[k])
        return out

    # ------------------------------------------------------------------ #
    # Агрегаты period_aggregates (версионировано, §3.5)
    # ------------------------------------------------------------------ #
    _AGG_JSON_COLS = ("volume_7d", "volume_28d", "decoupling", "hr_recovery",
                      "pace_by_hr_grid", "gct_by_pace_grid", "provenance")

    def put_aggregate(self, period_key: str, algo_version: str, agg: dict) -> None:
        """Записать строку period_aggregates. JSON-поля сериализуются, max_hr — int."""
        row = {
            "period_key": period_key,
            "algo_version": algo_version,
            "max_hr_accumulated": agg.get("max_hr_accumulated"),
            "computed_at": _iso_now(),
        }
        for k in self._AGG_JSON_COLS:
            row[k] = json.dumps(agg.get(k), ensure_ascii=False)
        cols = ("period_key", "algo_version", "max_hr_accumulated", "computed_at",
                *self._AGG_JSON_COLS)
        ph = ",".join("?" for _ in cols)
        upd = ",".join(f"{c}=excluded.{c}" for c in cols
                       if c not in ("period_key", "algo_version"))
        self.conn.execute(
            f"INSERT INTO period_aggregates ({','.join(cols)}) VALUES ({ph}) "
            f"ON CONFLICT(period_key,algo_version) DO UPDATE SET {upd}",
            tuple(row[c] for c in cols),
        )
        self.conn.commit()

    def get_aggregate(self, period_key: str, algo_version: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM period_aggregates WHERE period_key=? AND algo_version=?",
            (period_key, algo_version),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        for k in self._AGG_JSON_COLS:
            if out.get(k):
                out[k] = json.loads(out[k])
        return out

    def all_aggregates(self, algo_version: str) -> list[dict]:
        """Все периоды одной версии, по возрастанию period_key (для динамики §5.4)."""
        rows = self.conn.execute(
            "SELECT * FROM period_aggregates WHERE algo_version=? ORDER BY period_key",
            (algo_version,),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            for k in self._AGG_JSON_COLS:
                if d.get(k):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out

    def activity_ids(self, *, start: Optional[str] = None, end: Optional[str] = None,
                     sport: Optional[str] = None, limit: Optional[int] = None,
                     order_desc: bool = True) -> list[int]:
        """ID каталога по фильтру дат/спорта — для батчей обогащения и query_index."""
        clauses, params = [], []
        if start:
            clauses.append("date >= ?"); params.append(start)
        if end:
            clauses.append("date <= ?"); params.append(end)
        if sport:
            clauses.append("sport = ?"); params.append(sport)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if order_desc else "ASC"
        lim = f" LIMIT {int(limit)}" if limit else ""
        sql = f"SELECT activity_id FROM activities{where} ORDER BY date {order}{lim}"
        return [r[0] for r in self.conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------------ #
    # Сырьё activity_raw (zlib JSON)
    # ------------------------------------------------------------------ #
    def put_raw(self, activity_id: int, kind: str, payload: Any) -> None:
        self.conn.execute(
            "INSERT INTO activity_raw(activity_id,kind,payload,fetched_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(activity_id,kind) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at",
            (activity_id, kind, _compress(payload), _iso_now()),
        )
        self.conn.commit()

    def backfill_device_model(self, activity_id: int) -> None:
        """Заполняет device_model из сохранённого summary_json (deviceId), без сети.
        Факт железа для группировки (§5.4). Идемпотентно. NULL если deviceId нет.
        Существующий каталог мог быть создан со старым None — дозаполняем при обогащении."""
        row = self.conn.execute(
            "SELECT summary_json, device_model FROM activities WHERE activity_id=?",
            (activity_id,),
        ).fetchone()
        if not row or not row["summary_json"]:
            return
        try:
            dev = json.loads(row["summary_json"]).get("deviceId")
        except (json.JSONDecodeError, AttributeError, TypeError):
            return
        if dev is not None and row["device_model"] != str(dev):
            self.conn.execute(
                "UPDATE activities SET device_model=? WHERE activity_id=?",
                (str(dev), activity_id),
            )

    def get_raw(self, activity_id: int, kind: str) -> Optional[Any]:
        row = self.conn.execute(
            "SELECT payload FROM activity_raw WHERE activity_id=? AND kind=?",
            (activity_id, kind),
        ).fetchone()
        return _decompress(row["payload"]) if row else None

    # ------------------------------------------------------------------ #
    # user_data — рукотворные метки (этап 7, §3.6). Намерение изолировано от
    # recompute; привязка (раствор) версионируема в user_lactate_resolved.
    # ------------------------------------------------------------------ #
    def add_user_lactate(self, activity_id: int, mmol: float,
                         at_time: Optional[int] = None,
                         user_ref: Optional[str] = None,
                         source: str = "llm",
                         validation: str = "ok",
                         lap_count: Optional[int] = None) -> int:
        """Записать НАМЕРЕНИЕ лактатной метки. Возвращает вечный mark_id.

        Привязка (lap/hr_at/pace_at) здесь НЕ вычисляется — это раствор, живёт в
        user_lactate_resolved, версионируется, резолвится из streams (enrich).
        at_time — wall-clock UTC мс (НЕ elapsed), см. миграцию v4.

        validation/lap_count store ПРИНИМАЕТ готовыми, НЕ вычисляет (тупой слой):
        их считает вызывающий (тул при немедленном резолве / sync при recompute),
        у кого laps на руках. id-чек «activity_id в каталоге» — тоже в туле, не тут.
        validation: 'ok' | 'deferred' | 'invalid' (см. миграцию v5). lap_count —
        доказательство invalid («кругов M»), NULL если неприменимо/ещё не вычислен.
        """
        cur = self.conn.execute(
            "INSERT INTO user_data(activity_id,kind,mmol,user_ref,at_time,source,"
            "created_at,validation,lap_count) VALUES(?,?,?,?,?,?,?,?,?)",
            (activity_id, "lactate", float(mmol), user_ref, at_time, source,
             _iso_now(), validation, lap_count),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_note(self, activity_id: int, text: str, source: str = "llm") -> int:
        """Записать заметку (свободный текст, контекст для LLM). Возвращает mark_id.
        Не резолвится — раствора у заметки нет. validation='ok' (DEFAULT): у заметки
        нет user_ref/at_time → структурно валидировать нечего (deferred/invalid
        неприменимы). НО id-чек «activity_id в каталоге» делает вызывающий тул ДО
        этого вызова — иначе осиротевшая вечная заметка на галлюцинированном id."""
        cur = self.conn.execute(
            "INSERT INTO user_data(activity_id,kind,note_text,source,created_at) "
            "VALUES(?,?,?,?,?)",
            (activity_id, "note", text, source, _iso_now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def delete_user_mark(self, mark_id: int) -> bool:
        """Жёсткое удаление метки по mark_id. Каскадом чистит user_lactate_resolved
        (FK ON DELETE CASCADE, foreign_keys=ON). История правок не ведётся: метка
        либо есть, либо нет. Возвращает True если строка была удалена."""
        cur = self.conn.execute("DELETE FROM user_data WHERE mark_id=?", (mark_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_validation(self, mark_id: int, validation: str,
                       lap_count: Optional[int] = None) -> None:
        """Обновить вердикт валидности намерения (validation=f(текущие laps)).
        Зовётся sync-веткой recompute: пересчитала validation из laps → пишет сюда.
        validation БЕЗверсионна (зависит от laps, не от резолвера) — одна колонка,
        не ×algo_version. lap_count едет вместе (доказательство вердикта, тот же f)."""
        self.conn.execute(
            "UPDATE user_data SET validation=?, lap_count=? WHERE mark_id=?",
            (validation, lap_count, mark_id),
        )
        self.conn.commit()

    def get_user_data(self, activity_id: int) -> list[dict]:
        """Сырые НАМЕРЕНИЯ по активности (без раствора), включая validation/lap_count.
        Для sync/тула: пересчитать validation из текущих laps, резолвить ok из streams.
        """
        rows = self.conn.execute(
            "SELECT mark_id,activity_id,kind,mmol,user_ref,at_time,note_text,"
            "source,created_at,validation,lap_count "
            "FROM user_data WHERE activity_id=? ORDER BY mark_id",
            (activity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def put_user_lactate_resolved(self, mark_id: int, algo_version: str,
                                  lap: Optional[int], hr_at: Optional[float],
                                  pace_at: Optional[float]) -> None:
        """Записать РАСТВОР (привязку) метки под версией. Идемпотентно по
        (mark_id, algo_version). mark_id — уже существующий ключ из user_data;
        эта таблица его НЕ порождает (идентичность в намерении, не в растворе)."""
        self.conn.execute(
            "INSERT INTO user_lactate_resolved(mark_id,algo_version,lap,hr_at,pace_at,computed_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(mark_id,algo_version) DO UPDATE SET "
            "lap=excluded.lap, hr_at=excluded.hr_at, pace_at=excluded.pace_at, "
            "computed_at=excluded.computed_at",
            (mark_id, algo_version, lap, hr_at, pace_at, _iso_now()),
        )
        self.conn.commit()

    def clear_user_lactate_resolved(self, algo_version: str) -> int:
        """Снести ВЕСЬ раствор данной версии (полный recompute-проход: DELETE версии
        → INSERT заново). Намерение (user_data) не участвует — read-only. Возвращает
        число удалённых строк."""
        cur = self.conn.execute(
            "DELETE FROM user_lactate_resolved WHERE algo_version=?", (algo_version,)
        )
        self.conn.commit()
        return cur.rowcount

    def delete_user_lactate_resolved(self, mark_id: int, algo_version: str) -> bool:
        """ТОЧЕЧНЫЙ снос раствора ОДНОЙ метки под версией. Нужен для инварианта
        «invalid не несёт привязку»: метка ушла в invalid → sync снимает её раствор,
        не трогая раствор валидных меток той же версии (в отличие от clear_ по всей
        версии). Возвращает True если строка была."""
        cur = self.conn.execute(
            "DELETE FROM user_lactate_resolved WHERE mark_id=? AND algo_version=?",
            (mark_id, algo_version),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_user_marks_resolved(self, activity_id: int,
                                algo_version: str) -> list[dict]:
        """НАМЕРЕНИЕ ⨝ РАСТВОР по (mark_id, текущая algo_version) — для compact/full.
        Streams/laps НЕ читает: статус берётся из ХРАНИМОГО validation, привязка — из
        resolved. Расщепляет status по происхождению (не молчит — §3.5.2):
          resolved           — validation='ok' и раствор текущей версии есть;
          pending_resolve    — validation='ok', раствора нет (ждёт streams);
          pending_validation — validation='deferred' (user_ref, laps нет → N недоказуем);
          invalid            — validation='invalid' (круга N нет), с lap_count («кругов M»),
                               БЕЗ привязки (invalid не несёт раствор, даже если строка
                               залежалась — отдаём честный вердикт, не привязку).
        Заметки идут как есть (раствора/валидации структурной у них нет)."""
        rows = self.conn.execute(
            "SELECT u.mark_id, u.kind, u.mmol, u.user_ref, u.at_time, u.note_text, "
            "u.source, u.created_at, u.validation, u.lap_count, "
            "r.lap, r.hr_at, r.pace_at "
            "FROM user_data u "
            "LEFT JOIN user_lactate_resolved r "
            "  ON r.mark_id = u.mark_id AND r.algo_version = ? "
            "WHERE u.activity_id = ? ORDER BY u.mark_id",
            (algo_version, activity_id),
        ).fetchall()
        out = []
        for r in rows:
            if r["kind"] == "note":
                out.append({"mark_id": r["mark_id"], "kind": "note",
                            "text": r["note_text"], "source": r["source"],
                            "created_at": r["created_at"]})
                continue
            # kind == lactate
            mark = {"mark_id": r["mark_id"], "kind": "lactate", "mmol": r["mmol"],
                    "at_time": r["at_time"], "user_ref": r["user_ref"],
                    "source": r["source"], "created_at": r["created_at"]}
            v = r["validation"]
            if v == "invalid":
                # вердикт «круга N нет» + доказательство «кругов M». Привязку НЕ отдаём,
                # даже если resolved-строка залежалась (invalid не несёт раствор).
                mark["status"] = "invalid"
                mark["lap_count"] = r["lap_count"]
            elif v == "deferred":
                mark["status"] = "pending_validation"   # ждёт laps для проверки N
            else:  # v == 'ok'
                has_resolved = r["lap"] is not None or r["hr_at"] is not None \
                    or r["pace_at"] is not None
                if has_resolved:
                    mark["status"] = "resolved"
                    mark["lap"] = r["lap"]
                    mark["hr_at"] = r["hr_at"]
                    mark["pace_at"] = r["pace_at"]
                else:
                    mark["status"] = "pending_resolve"   # провалидирован, ждёт streams
            out.append(mark)
        return out

    # ------------------------------------------------------------------ #
    # Статус (для cache_status / CLI)
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        lo, hi = self.activity_date_range()
        raw_cnt = self.conn.execute("SELECT COUNT(*) FROM activity_raw").fetchone()[0]
        enr_cnt = self.conn.execute("SELECT COUNT(*) FROM activity_enriched").fetchone()[0]
        return {
            "schema_version": self.schema_version,
            "algo_version": self.meta_get("algo_version"),
            "download_policy": self.meta_get("download_policy"),
            "activities": self.activity_count(),
            "garmin_range": [lo, hi],
            "raw_rows": raw_cnt,
            "enriched_rows": enr_cnt,
            "last_sync": self.meta_get("last_sync"),
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# Маппинг сырой сводки Garmin → строка каталога.
# Дешёвые поля извлекаем; device/source оставляем NULL — их присваивает
# классификация на этапе обогащения (§3.2, не угадываем здесь). summary_json
# всегда сохраняем целиком, чтобы ничего не потерять.
# --------------------------------------------------------------------------- #
def activity_row_from_summary(summary: dict) -> dict:
    def g(*keys):
        for k in keys:
            if k in summary and summary[k] is not None:
                return summary[k]
        return None

    start = g("startTimeGMT", "startTimeLocal")
    date = start.split(" ")[0].split("T")[0] if isinstance(start, str) else None
    sport = None
    at = summary.get("activityType")
    if isinstance(at, dict):
        sport = at.get("typeKey")
    # флаг наличия бегового датчика: есть осмысленные биомех-поля в сводке
    gct = g("avgGroundContactTime")
    vr = g("avgVerticalRatio")
    has_biomech = 1 if (gct is not None or vr is not None) else 0
    return {
        "activity_id": g("activityId"),
        "date": date,
        "start_time": start,
        "sport": sport,
        "duration_s": g("duration", "elapsedDuration"),
        "distance_m": g("distance"),
        "moving_time_s": g("movingDuration"),  # уточняется обогащением (§6)
        "lap_count": g("lapCount"),
        "max_hr": g("maxHR"),
        # надёжный фильтруемый каденс (без _raw, см. ТЗ §3.1/§5.1)
        "avg_cadence": g("averageRunningCadenceInStepsPerMinute"),
        # обманчивые средние — суффикс _raw, не фильтруются
        "avg_hr_raw": g("averageHR"),
        "avg_speed_raw": g("averageSpeed"),
        # флаги наличия датчика, не аналитика
        "avg_gct": gct,
        "avg_vert_ratio": vr,
        # чужая производная Garmin — помечена, не фильтр
        "garmin_training_load_derived": g("activityTrainingLoad"),
        # флаги достоверности §3.2 (gps_validated уточняется позже — пока NULL)
        "has_biomech_sensor": has_biomech,
        "gps_validated": None,
        # deviceId — идентификатор ЧАСОВ (числовой), факт группировки «какое железо».
        # НЕ граница источника пульса: одни и те же часы работают и с нагрудником, и без
        # (проверено на архиве — оба deviceId имеют и chest, и non-chest тренировки).
        # §5.4: граница по hr_source, НЕ по device_model (смена часов при том же
        # нагруднике пульс не ломает). Кладём как факт, hr_source считается отдельно (enrich).
        "device_model": (str(summary.get("deviceId"))
                         if summary.get("deviceId") is not None else None),
        "hr_source": None,   # заполняется обогащением из потока (enrich-0.6.0), не здесь
        "gps_type": None,
        "biomech_source": None,
        "summary_json": json.dumps(summary, ensure_ascii=False),
    }


if __name__ == "__main__":
    # самопроверка: создать временную БД, прогнать миграции, вставить строку
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        st = Store(Path(d) / "t.db")
        assert st.schema_version == SCHEMA_VERSION, st.schema_version
        row = activity_row_from_summary({
            "activityId": 123, "startTimeGMT": "2026-06-27 10:00:15",
            "activityType": {"typeKey": "running"}, "distance": 12170.0,
            "duration": 3600.0, "maxHR": 171,
            "averageHR": 134, "averageSpeed": 3.4,
            "averageRunningCadenceInStepsPerMinute": 178,
            "avgGroundContactTime": 240, "avgVerticalRatio": 7.1,
            "activityTrainingLoad": 210,
        })
        assert row["avg_cadence"] == 178, row["avg_cadence"]
        assert row["avg_hr_raw"] == 134
        assert row["avg_speed_raw"] == 3.4
        assert row["has_biomech_sensor"] == 1, row["has_biomech_sensor"]
        assert row["garmin_training_load_derived"] == 210
        st.upsert_activities([row])
        st.put_raw(123, "laps", {"lapDTOs": [{"x": 1}]})
        assert st.get_raw(123, "laps") == {"lapDTOs": [{"x": 1}]}
        st.meta_set("algo_version", "0.0.0")
        st.meta_set("download_policy", "all")

        # --- этап 7: user_data (намерение) + user_lactate_resolved (раствор) ---
        assert st.schema_version == 5, st.schema_version
        ver = "test-0.0.0"
        # намерение: at_time-метка (ok), user_ref без laps (deferred), заметка
        m1 = st.add_user_lactate(123, 6.0, at_time=1_700_000_150_000, source="llm")  # validation='ok'
        m2 = st.add_user_lactate(123, 3.3, user_ref="lap4", source="manual",
                                 validation="deferred")  # laps не было → deferred
        n1 = st.add_note(123, "интервалка 4x1км, лактат после 4-го", source="llm")
        assert m2 > m1 and n1 > m2, (m1, m2, n1)      # AUTOINCREMENT монотонен
        rows = st.get_user_data(123)
        assert len(rows) == 3, rows
        assert [r["kind"] for r in rows] == ["lactate", "lactate", "note"], rows
        assert rows[0]["validation"] == "ok" and rows[1]["validation"] == "deferred"
        assert rows[2]["validation"] == "ok"           # заметка: DEFAULT 'ok'
        assert rows[0]["at_time"] == 1_700_000_150_000 and rows[1]["user_ref"] == "lap4"

        # четыре состояния чтения (status из ХРАНИМОГО validation):
        marks = st.get_user_marks_resolved(123, ver)
        by_id = {mk["mark_id"]: mk for mk in marks}
        assert by_id[m1]["status"] == "pending_resolve", by_id[m1]   # ok, раствора нет
        assert by_id[m2]["status"] == "pending_validation", by_id[m2]  # deferred (нет laps)
        assert by_id[n1]["kind"] == "note" and "status" not in by_id[n1]
        assert by_id[n1]["text"].startswith("интервалка")

        # раствор m1 → resolved; m2 всё ещё pending_validation
        st.put_user_lactate_resolved(m1, ver, lap=4, hr_at=185.0, pace_at=205.0)
        by_id = {mk["mark_id"]: mk for mk in st.get_user_marks_resolved(123, ver)}
        assert by_id[m1]["status"] == "resolved" and by_id[m1]["hr_at"] == 185.0
        assert by_id[m2]["status"] == "pending_validation", by_id[m2]
        # раствор виден ТОЛЬКО под своей версией
        assert st.get_user_marks_resolved(123, "other-ver")[0]["status"] == "pending_resolve"

        # deferred → laps дозакачались, круга 4 НЕТ (laps показали 3) → invalid + lap_count
        st.set_validation(m2, "invalid", lap_count=3)
        by_id = {mk["mark_id"]: mk for mk in st.get_user_marks_resolved(123, ver)}
        assert by_id[m2]["status"] == "invalid", by_id[m2]
        assert by_id[m2]["lap_count"] == 3, "invalid несёт доказательство «кругов M»"
        assert "hr_at" not in by_id[m2], "invalid НЕ несёт привязку"

        # invalid НЕ терминальна: laps пересобрались, круг 4 теперь есть → ok, оживает
        st.set_validation(m2, "ok", lap_count=None)
        assert st.get_user_marks_resolved(123, ver)[1]["status"] == "pending_resolve", \
            "invalid→ok: метка ожила (ждёт streams), не мертва"

        # инвариант «invalid не несёт привязку»: даже если раствор ЗАЛЕЖАЛСЯ, при invalid
        # get_user_marks_resolved его не отдаёт (честный вердикт, не привязка)
        st.put_user_lactate_resolved(m2, ver, lap=4, hr_at=170.0, pace_at=None)
        st.set_validation(m2, "invalid", lap_count=3)
        stale = st.get_user_marks_resolved(123, ver)[1]
        assert stale["status"] == "invalid" and "hr_at" not in stale, "залежавшийся раствор при invalid не отдаётся"
        # точечный снос раствора ОДНОЙ метки (sync снимет при уходе в invalid), не трогая m1
        assert st.delete_user_lactate_resolved(m2, ver) is True
        assert st.conn.execute("SELECT COUNT(*) FROM user_lactate_resolved WHERE mark_id=?",
                               (m2,)).fetchone()[0] == 0
        assert st.conn.execute("SELECT COUNT(*) FROM user_lactate_resolved WHERE mark_id=?",
                               (m1,)).fetchone()[0] == 1, "точечный снос m2 НЕ тронул раствор m1"

        # clear ВСЕЙ версии → у m1 раствора нет, статус по validation='ok' → pending_resolve
        assert st.clear_user_lactate_resolved(ver) == 1
        assert st.get_user_marks_resolved(123, ver)[0]["status"] == "pending_resolve"

        # жёсткое удаление + каскад
        st.put_user_lactate_resolved(m1, ver, lap=4, hr_at=185.0, pace_at=205.0)
        assert st.delete_user_mark(m2) is True
        assert len(st.get_user_data(123)) == 2
        assert st.conn.execute("SELECT COUNT(*) FROM user_lactate_resolved WHERE mark_id=?",
                               (m2,)).fetchone()[0] == 0
        # mark_id НЕ переиспользуется после удаления
        m3 = st.add_user_lactate(123, 4.0, at_time=1_700_000_200_000)
        assert m3 > n1, (m3, n1)

        import pprint
        pprint.pp(st.status())
        st.close()
        print("self-test OK")
