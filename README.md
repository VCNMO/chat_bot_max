# Портфолио педагога в мессенджере MAX

- собирает фотографии грамот за конкурсы и удостоверений о повышении
квалификации, 
- распознает документы
- задаёт уточняющие вопросы по каждому документу
- сохраняет в БД
- показывает итоговые данные за последний отчетный период.
Может использоваться при подготовки документов по эффективному контракту или стимулирующих выплатах.

Имя бота: ideya_fiks_187_bot

Ссылка на бота: https://max.ru/ideya_fiks_187_bot


## Документы:

грамота или диплом: название конкурса, организатор конкурса, уровень, дата проведения, результат.

документ о повышении квалификации: название курса, организатор, дата выдачи, количество часов.


`bot.py`

```
/start
  └─ ввод ФИО
       └─ [🏅 Добавить грамоту] [🎓 Добавить курс ПК]
            └─ «Пришлите фото документа» (📎 → Камера)
                 └─ вопросы по документу
                      └─ сводка за отчетный период + те же кнопки
```

Команды: 

`/start` — начать/показать сводку

`/my` — сводка

`/cancel` — отменить незаконченный документ.


## База данных

Файл `data/documents.sqlite3` (SQLite, WAL). Фото документа хранится прямо в БД как BLOB.
Схема общая для v1 и v2.

```sql
CREATE TABLE users (
    user_id      INTEGER PRIMARY KEY,
    fio          TEXT,
    state        TEXT,      -- new | menu | await_photo | fill
    pending_kind TEXT,      -- competition | course
    draft_id     INTEGER,   -- незаконченный документ
    created_at   TEXT
);

CREATE TABLE documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    kind        TEXT,       -- competition | course
    photo       BLOB,       -- сам снимок документа
    photo_mime  TEXT,
    title       TEXT,       -- конкурс или курс
    organizer   TEXT,
    level       TEXT,       -- только для конкурса
    dates       TEXT,
    year        INTEGER,
    result      TEXT,       -- только для конкурса
    hours       INTEGER,    -- только для КПК
    completed   INTEGER,    -- 1 = анкета заполнена
    created_at  TEXT
);
```







