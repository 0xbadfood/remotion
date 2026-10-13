"""Two-ears decision over the saved eval transcripts (run scripts/audio_arbiter_eval.py first; optional argument: the
case-set subfolder, e.g. holdout). Code first drops formatting (joined words, possessive, same number) and sound-alike
name spellings (consonant skeleton, accents stripped). A take then fails only where BOTH Whisper and Gemma missed the
same script word (nothing heard there sounds like it; filler words exempt, "not" is not filler) or both heard the
same extra word. A Gemma refusal ("Please provide the audio") makes Gemma unusable for that clip."""
import json,sys,re,collections
from difflib import SequenceMatcher
sys.path.insert(0,'scripts'); import audio_arbiter_eval as E
from infofilm.audio_audit import same_number
OUT=E.OUT/sys.argv[1] if len(sys.argv)>1 else E.OUT      # optional case-set subfolder (e.g. holdout)
cases={c['id']:c for c in json.load(open(OUT/'cases.json'))}
rows=json.load(open(OUT/'report.json'))['rows']
import unicodedata
def letters(s): return re.sub(r"[^a-z]","",unicodedata.normalize("NFKD",s.lower()).encode("ascii","ignore").decode())
def sound(s):
    s=letters(s)
    for a,b in (("ph","f"),("th","t"),("dh","d"),("ck","k"),("c","k"),("q","k"),("z","s"),("w","v"),("y","i")): s=s.replace(a,b)
    return re.sub(r"(.)\1+",r"\1",s)
def skeleton(s): return re.sub(r"(.)\1+",r"\1",re.sub(r"[aeiouh]","",sound(s)))
def ratio(a,b): return SequenceMatcher(None,a,b).ratio()
def similar(a,b,th=.7): return ratio(sound(a),sound(b))>=th
def benign(e,o):
    """Formatting or a sound-alike name, not a change of speech."""
    if letters(e)==letters(o) or letters(e.replace(" is","s"))==letters(o): return True
    if e and o and same_number({"expected":e,"observed":o}): return True
    le,lo=len(letters(e)),len(letters(o))
    if not (le and lo) or abs(le-lo)>.25*max(le,lo): return False            # 'hitler is not' is not 'hitlers'
    return ratio(skeleton(e),skeleton(o))>=.75                                 # Thanjavur / Tanjore, Kempelen / Campellen
def regions(exp,obs):
    """[(i,j,heard)]: script words exp[i:j] this ear heard as `heard`, departures only."""
    out=[]
    for tag,i,j,k,l in SequenceMatcher(None,exp,obs,autojunk=False).get_opcodes():
        if tag!='equal' and not benign(' '.join(exp[i:j]),' '.join(obs[k:l])): out.append((i,j,' '.join(obs[k:l])))
    return out
REFUSAL=re.compile(r"\b(provide the (audio|video)|i do not have access|i cannot (hear|access|listen)|no audio)\b",re.I)
FILLER={"the","a","an","and","of","to","in","on","at","is","it","its","that","this","as","by","for","with"}
def alike(w,x):
    return letters(w)==letters(x) or (len(letters(w))>2 and ratio(skeleton(w),skeleton(x))>=.75 and ratio(sound(w),sound(x))>=.5)
def missed(exp,regs):
    """script positions this ear did not hear: inside a departure and nothing heard there sounds like the word."""
    out=set()
    for i,j,heard in regs:
        words=heard.split()
        out.update(t for t in range(i,j) if exp[t] not in FILLER and not any(alike(exp[t],w) for w in words))
    return out
def extra(exp,regs):
    """(region, word) this ear heard that sounds like no script word in that region: invented speech."""
    out=[]
    for i,j,heard in regs:
        out+=[(i,j,w) for w in heard.split() if w not in FILLER and not any(alike(e,w) for e in exp[max(0,i-1):j+1])]
    return out
def decide(c,gemma_text):
    if not gemma_text or REFUSAL.search(gemma_text): return None,['gemma_no_transcript']
    ew=E.score(c,c['whisper_raw']); eg=E.score(c,gemma_text)
    exp=ew['expected_text'].split()
    rw,rg=regions(exp,ew['transcript_text'].split()),regions(exp,eg['transcript_text'].split())
    # A repeated word ("not not") is one place: each ear may drop a different copy.
    run=lambda t: next(s for s in range(t,-1,-1) if s==0 or exp[s-1]!=exp[t])
    real=sorted({exp[t] for t in missed(exp,rw) for u in missed(exp,rg) if run(t)==run(u)})   # both ears missed it
    real+=['+'+w for i,j,w in extra(exp,rw) for a,b,x in extra(exp,rg)
           if a<=j+1 and i<=b+1 and alike(w,x)]                                     # both heard the same extra word
    return not real, real[:6]
tally=collections.defaultdict(lambda:[0,0,0]); wrong=[]
for r in rows:
    c=cases[r['id']]; ok,why=decide(c,r['gemma_text'])
    t=tally[r['kind']]; t[0]+=1
    if ok is None: t[2]+=1; wrong.append((r['kind'],r.get('change'),r['run'][:22],r['point'],why)); continue
    t[1]+=ok
    if (r['kind']=='fake')==ok or (r['kind']!='fake' and not ok): wrong.append((r['kind'],r.get('change'),r['run'][:22],r['point'],why[:4]))
for k,v in tally.items(): print(f"{k:22} accepted {v[1]:2} of {v[0]:2}   gemma unusable {v[2]}")
for x in wrong: print(x)
