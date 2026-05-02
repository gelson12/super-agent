import json, sys, re

PY = '/c/Users/Gelson/AppData/Local/Programs/Python/Python312/python.exe'

def load(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)

def save(path, wf):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(wf, f, separators=(',', ':'), ensure_ascii=False)
    print(f"Saved {path}")

def get_node(wf, name):
    for n in wf.get('nodes', []):
        if n.get('name') == name:
            return n
    return None

def read_js(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

# ═══════════════════════════════════════
# BTC MAIN
# ═══════════════════════════════════════
print("=== Patching btc_main.json ===")
btc = load('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/btc_main.json')

# 1. Execute Trade rewrite
et = get_node(btc, 'Execute Trade')
new_et = read_js('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/_btc_execute_trade.js')
et['parameters']['jsCode'] = new_et
print("  [OK] Execute Trade replaced")

# 2. Decision Gate patches
dg = get_node(btc, 'Decision Gate')
code = dg['parameters']['jsCode']

# Fix lastSigs filter
old = "mems.filter(m=>!(m.content||'').includes('ADAPTIVE_PARAMS')).slice(0,3)"
new = "mems.filter(m=>!(m.content||'').includes('ADAPTIVE_PARAMS')&&!(m.content||'').includes('POSITION_STATE')).slice(0,3)"
if old in code:
    code = code.replace(old, new)
    print("  [OK] lastSigs filter fixed")
else:
    print("  [WARN] lastSigs pattern not found")

# Add ETH cross-confirmation after staleness decay
decay_line = "if(allSame&&priceMove<0.8&&!noDecayR.includes(reg.regime)){conf*=0.75;decayApplied=true;}"
eth_cross = (
    "let ethCross='none';"
    "try{const ethR=$('Fetch ETH').first().json;"
    "const ek=Object.keys((ethR&&ethR.result)||{})[0];"
    "const et=((ethR&&ethR.result)||{})[ek]||{};"
    "const ep=et.c?parseFloat(et.c[0]):0;"
    "const eo=et.o?parseFloat(et.o[1]):0;"
    "const echg=eo>0?(ep-eo)/eo*100:0;"
    "const ed=echg>0.5?'BUY':echg<-0.5?'SELL':null;"
    "if(ed&&direction!=='WAIT'){"
    "if(ed===direction){conf=Math.min(95,conf*1.08);ethCross='confirms';}"
    "else{conf*=0.92;ethCross='diverges';}}"
    "}catch(e){}"
)
if decay_line in code:
    code = code.replace(decay_line, decay_line + eth_cross)
    print("  [OK] ETH cross-confirmation added")
else:
    print("  [WARN] decay_line not found")

# Add ethCross to return statement
old_ret = 'return [{json:{direction:finalDir,confidence:conf,rawScore:raw,send_alert:sendAlert,regime:reg.regime,bayesianProb:pred.bayesianProb,ev:risk.ev,rr:risk.rr,quorum,quorumPass,noTradeScore,newsShock,decayApplied,skipClaude,minConf,buyMult,sellMult,adaptiveActive}}];'
new_ret = 'return [{json:{direction:finalDir,confidence:conf,rawScore:raw,send_alert:sendAlert,regime:reg.regime,bayesianProb:pred.bayesianProb,ev:risk.ev,rr:risk.rr,quorum,quorumPass,noTradeScore,newsShock,decayApplied,skipClaude,minConf,buyMult,sellMult,adaptiveActive,ethCross}}];'
if old_ret in code:
    code = code.replace(old_ret, new_ret)
    print("  [OK] Return updated with ethCross")
else:
    print("  [WARN] return statement not found")

dg['parameters']['jsCode'] = code
save('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/btc_main.json', btc)

# ═══════════════════════════════════════
# ETH MAIN
# ═══════════════════════════════════════
print("\n=== Patching eth_main.json ===")
eth = load('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/eth_main.json')

# 1. Execute Trade rewrite
et2 = get_node(eth, 'Execute Trade')
new_et2 = read_js('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/_eth_execute_trade.js')
et2['parameters']['jsCode'] = new_et2
print("  [OK] Execute Trade replaced")

# 2. Decision Gate patches
dg2 = get_node(eth, 'Decision Gate')
code2 = dg2['parameters']['jsCode']

# Fix lastSigs filter
if old in code2:
    code2 = code2.replace(old, new)
    print("  [OK] lastSigs filter fixed")
else:
    print("  [WARN] lastSigs pattern not found")

# Add BTC cross-confirmation (ETH workflow has Fetch BTC Corr node)
btc_cross = (
    "let btcCross='none';"
    "try{const btcR=$('Fetch BTC Corr').first().json;"
    "const bk=Object.keys((btcR&&btcR.result)||{})[0];"
    "const bt=((btcR&&btcR.result)||{})[bk]||{};"
    "const bp=bt.c?parseFloat(bt.c[0]):0;"
    "const bo=bt.o?parseFloat(bt.o[1]):0;"
    "const bchg=bo>0?(bp-bo)/bo*100:0;"
    "const bd=bchg>0.5?'BUY':bchg<-0.5?'SELL':null;"
    "if(bd&&direction!=='WAIT'){"
    "if(bd===direction){conf=Math.min(95,conf*1.08);btcCross='confirms';}"
    "else{conf*=0.92;btcCross='diverges';}}"
    "}catch(e){}"
)
if decay_line in code2:
    code2 = code2.replace(decay_line, decay_line + btc_cross)
    print("  [OK] BTC cross-confirmation added")
else:
    print("  [WARN] decay_line not found in ETH DG")

# Add btcCross to return statement
old_ret2 = old_ret  # same structure
new_ret2 = old_ret.replace('adaptiveActive}}];', 'adaptiveActive,btcCross}}];')
if old_ret2 in code2:
    code2 = code2.replace(old_ret2, new_ret2)
    print("  [OK] Return updated with btcCross")
else:
    print("  [WARN] return statement not found in ETH DG")

dg2['parameters']['jsCode'] = code2
save('c:/Users/Gelson/Downloads/super-agent/n8n_workflows/eth_main.json', eth)

print("\n=== DONE ===")
