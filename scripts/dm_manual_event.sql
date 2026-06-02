-- Тип события для ручных команд DeployManager.
-- DeployManager при start/stop/restart создаёт строку в dispatcher.service_error_log
-- (handled=true, error_code='DM_MANUAL') и кладёт её id в watchdog_instruction.log_id,
-- чтобы агент-watchdog после успешного выполнения смог записать error_handling_log
-- (там error_log_id NOT NULL). handled=true держит строку вне service_error_view → GD её
-- не подхватывает (ни инструкций-двойников, ни алертов).
-- service_error_log.error_code → FK на dispatcher.error_types, поэтому код нужно завести тут.
INSERT INTO dispatcher.error_types (error_code, description)
VALUES ('DM_MANUAL',
        'Ручная команда start/stop/restart через DeployManager (служебное событие, handled=true).')
ON CONFLICT (error_code) DO NOTHING;