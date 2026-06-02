-- Разрешить source='dm' (DeployManager) в очереди команд диспетчера.
-- Было: CHECK (source IN ('cron','gd','manual')) — наш source='dm' не проходил.
-- DeployManager ставит start/stop/restart в dispatcher.watchdog_instruction с source='dm',
-- чтобы отличать команды этого инструмента от 'gd' (GlobalDispatcher) / 'cron' / 'manual'.
ALTER TABLE dispatcher.watchdog_instruction
    DROP CONSTRAINT IF EXISTS watchdog_instruction_source_check;
ALTER TABLE dispatcher.watchdog_instruction
    ADD CONSTRAINT watchdog_instruction_source_check
    CHECK (source IN ('cron', 'gd', 'manual', 'dm'));