import json, sys
sys.stdout.reconfigure(encoding='utf-8')

d = json.load(open('n8n/bridge_chief_of_staff_bot.json', encoding='utf-8'))

# ── 1. Build task payload: detect APPROVE/REJECT from Telegram ─────────────
build_node = next(n for n in d['nodes'] if n.get('name') == 'Build task payload')
code = build_node['parameters']['jsCode']

OLD_TG = (
    "if (src.json && src.json.message && src.json.message.chat) {\n"
    "    // Telegram trigger\n"
    "    task = 'user_dm';\n"
    "    let _utext = src.json.message.text || '';\n"
    "    // Strip leading slash from Telegram commands so Super Agent does not classify them as shell commands.\n"
    "    if (_utext.startsWith('/')) {\n"
    "        const parts = _utext.slice(1).split(/\\s+/);\n"
    "        const cmd = (parts[0] || '').toLowerCase();\n"
    "        const rest = parts.slice(1).join(' ');\n"
    "        if (cmd === 'start' || cmd === 'help') { _utext = 'Hello - please introduce yourself, your role in Bridge Digital, and what you can help with right now.'; }\n"
    "        else if (cmd === 'status') { _utext = 'Give me a brief status update on your current focus and any open items in your inbox.'; }\n"
    "        else { _utext = (cmd + ' ' + rest).trim(); }\n"
    "    }\n"
    "    user_text = _utext;\n"
    "    chat_id = src.json.message.chat.id;\n"
    "    from_agent = 'user';"
)

NEW_TG = (
    "if (src.json && src.json.message && src.json.message.chat) {\n"
    "    let _utext = src.json.message.text || '';\n"
    "    // Detect APPROVE/REJECT improvement proposal commands\n"
    "    const _approveMatch = _utext.match(/^(APPROVE|REJECT)\\s+([\\w-]+)/i);\n"
    "    if (_approveMatch) {\n"
    "        task = 'proposal_decision';\n"
    "        user_text = JSON.stringify({ decision: _approveMatch[1].toUpperCase(), proposal_id: _approveMatch[2], raw: _utext });\n"
    "        chat_id = src.json.message.chat.id;\n"
    "        from_agent = 'user';\n"
    "    } else {\n"
    "        task = 'user_dm';\n"
    "        if (_utext.startsWith('/')) {\n"
    "            const parts = _utext.slice(1).split(/\\s+/);\n"
    "            const cmd = (parts[0] || '').toLowerCase();\n"
    "            const rest = parts.slice(1).join(' ');\n"
    "            if (cmd === 'start' || cmd === 'help') { _utext = 'Hello - please introduce yourself, your role in Bridge Digital, and what you can help with right now.'; }\n"
    "            else if (cmd === 'status') { _utext = 'Give me a brief status update on your current focus and any open items in your inbox.'; }\n"
    "            else { _utext = (cmd + ' ' + rest).trim(); }\n"
    "        }\n"
    "        user_text = _utext;\n"
    "        chat_id = src.json.message.chat.id;\n"
    "        from_agent = 'user';\n"
    "    }"
)

assert OLD_TG in code, "OLD_TG not found in Build task payload"
code = code.replace(OLD_TG, NEW_TG)
build_node['parameters']['jsCode'] = code
print("Build task payload: APPROVE/REJECT detection added")

# ── 2. Assemble prompt: proposal handling rules + apply_context action ──────
assemble_node = next(n for n in d['nodes'] if n.get('name') == 'Assemble prompt')
code = assemble_node['parameters']['jsCode']

OLD_OUTPUT = (
    'no_op",    "payload": {"reason": "..."}}\\n  ]\\n}\\n'
    'Keep actions <= 5. Never include non-whitelisted action types. User-facing commentary belongs in reply_text, not in alert actions.`'
)

NEW_OUTPUT = (
    'no_op",    "payload": {"reason": "..."}}},\\n'
    '    {"type": "apply_context", "payload": {"bot_name": "<bot>", "context_hint": "<one-sentence directive>", "proposal_id": "<uuid-or-empty>"}}\\n'
    '  ]\\n'
    '}\\n'
    'Keep actions <= 5. Never include non-whitelisted action types.\\n\\n'
    'BOT IMPROVEMENT PROPOSALS (memo_type=bot_improvement_proposal):\\n'
    '  1. reply_text: explain proposal plainly + "Reply APPROVE <proposal_id> or REJECT <proposal_id> to decide."\\n'
    '  2. Archive the memo.\\n\\n'
    'PROPOSAL DECISION (task=proposal_decision):\\n'
    '  APPROVE -> apply_context action with bot_name+context_hint from original proposal. Archive proposal memo.\\n'
    '  REJECT -> archive with reason rejected_by_human. Confirm to human.\\n'
    '  NEVER apply context without explicit human APPROVE.`'
)

assert OLD_OUTPUT in code, "OLD_OUTPUT not found in Assemble prompt"
code = code.replace(OLD_OUTPUT, NEW_OUTPUT)

# Extend taskBlock for proposal_decision
OLD_TASKBLOCK = ": `An inter-agent invocation arrived from '${task.from_agent}': ${task.user_text || 'no message body'}. Decide how to respond.`;\n\nconst outputGuard"
NEW_TASKBLOCK = (
    ": task.task === 'proposal_decision'\n"
    "      ? (() => { try { const pd = JSON.parse(task.user_text || '{}'); return `The human replied ${pd.decision} for proposal ${pd.proposal_id}. Process: APPROVE -> find the bot_improvement_proposal memo in your inbox, issue apply_context action with bot_name and proposed_change as context_hint, archive the memo. REJECT -> archive with reason rejected_by_human. Confirm via reply_text.`; } catch(e) { return 'Process proposal decision: ' + task.user_text; } })()\n"
    "      : `An inter-agent invocation arrived from '${task.from_agent}': ${task.user_text || 'no message body'}. Decide how to respond.`;\n\nconst outputGuard"
)

assert OLD_TASKBLOCK in code, "OLD_TASKBLOCK not found in Assemble prompt"
code = code.replace(OLD_TASKBLOCK, NEW_TASKBLOCK)

assemble_node['parameters']['jsCode'] = code
print("Assemble prompt: proposal handling + apply_context action added")

# ── 3. Execute low-risk action SQL: add apply_context CTE ───────────────────
exec_node = next(n for n in d['nodes'] if n.get('name') == 'Execute low-risk action')
old_sql = exec_node['parameters']['query']

OLD_SELECT = (
    "SELECT\n"
    "  (SELECT memo_id FROM memo_insert)  AS memo_created,\n"
    "  (SELECT memo_id FROM archive_memo) AS memo_archived,\n"
    "  (SELECT event_id FROM event_log)   AS event_logged;"
)

NEW_SELECT = (
    "apply_context AS (\n"
    "  INSERT INTO bridge.bot_context_overrides\n"
    "      (bot_name, context_hint, approved_by, proposal_id, updated_at)\n"
    "  SELECT\n"
    "    p->>'bot_name',\n"
    "    p->>'context_hint',\n"
    "    'human_via_cos',\n"
    "    NULLIF(p->>'proposal_id','')::uuid,\n"
    "    NOW()\n"
    "  FROM input\n"
    "  WHERE $3 = 'apply_context'\n"
    "    AND (p->>'bot_name') IS NOT NULL\n"
    "    AND (p->>'context_hint') IS NOT NULL\n"
    "  ON CONFLICT (bot_name) DO UPDATE SET\n"
    "    context_hint = EXCLUDED.context_hint,\n"
    "    approved_by  = EXCLUDED.approved_by,\n"
    "    proposal_id  = EXCLUDED.proposal_id,\n"
    "    updated_at   = NOW()\n"
    "  RETURNING bot_name AS ctx_applied_to\n"
    "),\n"
    "mark_proposal_applied AS (\n"
    "  UPDATE bridge.bot_improvement_proposals\n"
    "  SET status = 'applied', applied_at = NOW()\n"
    "  WHERE $3 = 'apply_context'\n"
    "    AND id = NULLIF((SELECT p->>'proposal_id' FROM input),'')::uuid\n"
    "  RETURNING id AS proposal_applied\n"
    ")\n"
    "SELECT\n"
    "  (SELECT memo_id FROM memo_insert)                  AS memo_created,\n"
    "  (SELECT memo_id FROM archive_memo)                 AS memo_archived,\n"
    "  (SELECT event_id FROM event_log)                   AS event_logged,\n"
    "  (SELECT ctx_applied_to FROM apply_context)         AS context_applied_to,\n"
    "  (SELECT proposal_applied FROM mark_proposal_applied) AS proposal_applied;"
)

assert OLD_SELECT in old_sql, f"OLD_SELECT not found in Execute SQL. First 200: {old_sql[:200]}"
exec_node['parameters']['query'] = old_sql.replace(OLD_SELECT, NEW_SELECT)
print("Execute low-risk action SQL: apply_context CTE added")

with open('n8n/bridge_chief_of_staff_bot.json', 'w', encoding='utf-8') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
print("CoS bot saved")
