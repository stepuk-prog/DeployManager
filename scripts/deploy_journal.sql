-- Журнал действий DeployManager (ключ — program_id). Применено в БД Program (схема dispatcher).
CREATE TABLE IF NOT EXISTS dispatcher.deploy_journal (
    id                 serial PRIMARY KEY,
    program_id         integer NOT NULL,
    node_id            integer,
    action             varchar NOT NULL,        -- deploy | add_server | uninstall | manage | state_check
    folder_deployed    boolean NOT NULL DEFAULT false,   -- залили в папку (rsync)
    service_installed  boolean NOT NULL DEFAULT false,   -- залили service (юнит в /etc)
    db_updated         boolean NOT NULL DEFAULT false,   -- обновили данные в БД (привязка/VERSION)
    result             varchar,                 -- ok | partial | fail | <шаг, где встало>
    commit             varchar,                 -- git SHA
    operator           varchar,                 -- кто запускал
    details            jsonb,                   -- доп. (ошибка/команда/проба)
    ts                 timestamp NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deploy_journal_program_idx ON dispatcher.deploy_journal(program_id, ts DESC);
