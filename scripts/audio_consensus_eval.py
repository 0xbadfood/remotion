"""Two-ears decision over the saved eval transcripts (run scripts/audio_arbiter_eval.py first): a take fails only where
Whisper and Gemma both depart from the same script word and heard the same thing there, unless code finds it is
formatting (joined words, possessive, same number) or a sound-alike spelling of the script word."""
import json,sys,re,collections
from difflib import SequenceMatcher
sys.path.insert(0,'scripts'); import audio_arbiter_eval as E
from infofilm.audio_audit import same_number
cases={c['id']:c for c in json.load(open(E.OUT/'cases.json'))}
rows=json.load(open(E.OUT/'report.json'))['rows']
REFUSAL=re.compile(r"\b(provide the audio|i do not have access|i cannot (hear|access|listen)|no audio)\b",re.I)
def letters(s): return re.sub(r"[^a-z]","",s)
def benign_span(e,o):
    """Formatting, not speech: joined words, possessive 's, same number."""
    if letters(e)==letters(o): return True
    if letters(e.replace(" is","s"))==letters(o): return True       # normalizer expands possessive 's to ' is'
    return bool(e and o) and same_number({"expected":e,"observed":o})
def heard(exp,obs):
    """script index -> word(s) this ear wrote there, for departures only."""
    out={}
    for tag,i,j,k,l in SequenceMatcher(None,exp,obs,autojunk=False).get_opcodes():
        if tag=='equal' or (tag=='replace' and benign_span(' '.join(exp[i:j]),' '.join(obs[k:l]))): continue
        if tag=='replace' and j-i!=l-k and similar(' '.join(exp[i:j]),' '.join(obs[k:l]),.75): continue   # joined name
        if tag=='insert': out[i-.5]='+'+' '.join(obs[k:l]); continue
        for t in range(i,j):
            a=k+(t-i)*(l-k)//(j-i); b=k+(t-i+1)*(l-k)//(j-i)
            out[t]=' '.join(obs[a:b])
    return out
def sound(s):
    s=letters(s)
    for a,b in (("ph","f"),("ck","k"),("c","k"),("q","k"),("z","s"),("w","v"),("y","i")): s=s.replace(a,b)
    return re.sub(r"(.)\1+",r"\1",s)
def similar(a,b,th): return SequenceMatcher(None,sound(a),sound(b)).ratio()>=th
def decide(c,gemma_text):
    if not gemma_text or REFUSAL.search(gemma_text): return None,['gemma_no_transcript']
    ew=E.score(c,c['whisper_raw']); eg=E.score(c,gemma_text)
    exp=ew['expected_text'].split()
    w,g=heard(exp,ew['transcript_text'].split()),heard(exp,eg['transcript_text'].split())
    real=[]
    for t in sorted(set(w)&set(g)):
        hw,hg=w[t],g[t]
        if not similar(hw,hg,.7): continue                       # ears disagree: neither is evidence of a change
        word=exp[int(t)] if t==int(t) else ''
        if word and hw and similar(word,hw,.7) and similar(word,hg,.7): continue   # sound-alike spelling
        real.append((word or '^',hw))
    return not real, real
tally=collections.defaultdict(lambda:[0,0,0]); wrong=[]
for r in rows:
    c=cases[r['id']]; ok,why=decide(c,r['gemma_text'])
    t=tally[r['kind']]; t[0]+=1
    if ok is None: t[2]+=1; wrong.append((r['kind'],r.get('change'),r['run'][:22],r['point'],why)); continue
    t[1]+=ok
    if (r['kind']=='fake')==ok or (r['kind']!='fake' and not ok): wrong.append((r['kind'],r.get('change'),r['run'][:22],r['point'],why[:4]))
for k,v in tally.items(): print(f"{k:22} accepted {v[1]:2} of {v[0]:2}   gemma unusable {v[2]}")
for x in wrong: print(x)
