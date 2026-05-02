-- CTO bot enable flag + vote_results table support
INSERT INTO bridge.system_limits (key, value)
VALUES ('cto_bot_enabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';

-- Ensure vote_request memo_type is handled by existing indexes
-- (no schema change needed — memo_type is free-form text)

-- Seed initial CTO authority level
INSERT INTO bridge.agent_performance (agent_name, date, authority_level)
VALUES ('cto', CURRENT_DATE, 9)
ON CONFLICT (agent_name, date) DO UPDATE SET authority_level = 9;
