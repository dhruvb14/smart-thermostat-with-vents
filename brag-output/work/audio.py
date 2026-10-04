import numpy as np, wave
SR = 48000; DUR = 21.0; N = int(SR*DUR)
t = np.arange(N)/SR
rng = np.random.default_rng(7)
def hz(m): return 440*2**((m-69)/12)
def env_adsr(n, a, r):
    e = np.ones(n); na=int(a*SR); nr=int(r*SR)
    e[:na] = np.linspace(0,1,na); e[-nr:] *= np.linspace(1,0,nr); return e
def tone(f, dur, harms=6, tilt=1.6, detune=0.0):
    n=int(dur*SR); tt=np.arange(n)/SR; s=np.zeros(n)
    for d in ([0] if detune==0 else [-detune, detune]):
        for h in range(1,harms+1):
            s += np.sin(2*np.pi*f*h*(1+d)*tt + rng.random()*6.28)/h**tilt
    return s
def add(buf, sig, at, g):
    i=int(at*SR); j=min(N,i+len(sig)); buf[i:j]+=sig[:j-i]*g

music=np.zeros(N); sfx=np.zeros(N)
# chords (midi) per section: D(sus2) | D | Bm | G | A->D
sections=[(0,3.4,[50,57,64,69],0.5),(3.4,6.6,[50,57,62,66,69],0.8),(6.6,9.8,[47,54,62,66,71],0.8),
          (9.8,13.0,[43,50,59,62,67],0.8),(13.0,17.0,[45,52,61,64,69],0.85),(17.0,21.0,[50,57,62,66,69,74],0.9)]
for a,b,notes,g in sections:
    d=b-a+0.6
    for m in notes:
        s=tone(hz(m), d, harms=5, tilt=1.8, detune=0.0025)*env_adsr(int(d*SR),0.35,0.7)
        add(music, s, a, g*0.05)
# bass, from reveal on
for a,b,notes,g in sections[1:]:
    root=notes[0]-12
    beat=0.6
    k=a
    while k < b-0.05:
        s=tone(hz(root),0.55,harms=3,tilt=2.2)*np.exp(-np.arange(int(0.55*SR))/SR*4)*env_adsr(int(0.55*SR),0.01,0.1)
        add(music,s,k,0.16); k+=beat
# soft kick on beats from 3.4, sidechain-ish
def kick():
    n=int(0.35*SR); tt=np.arange(n)/SR
    f=50+90*np.exp(-tt*30); ph=2*np.pi*np.cumsum(f)/SR
    return np.sin(ph)*np.exp(-tt*9)
k=3.4
while k<20.4:
    add(music,kick(),k,0.42 if k<20.0 else 0.3); k+=0.6
# hats on offbeats during product + safety
def hat():
    n=int(0.06*SR); w=rng.standard_normal(n); w=np.diff(np.concatenate([[0],w]))
    return w*np.exp(-np.arange(n)/SR*70)
k=6.6+0.3
while k<17.0:
    add(music,hat(),k,0.025); k+=0.6
# outro final ring
add(music, tone(hz(74),3.0,harms=4,tilt=2)*np.exp(-np.arange(int(3*SR))/SR*1.2), 17.05, 0.06)

# SFX: plucks in D major pentatonic
def pluck(m, dur=0.6, bright=2.0):
    n=int(dur*SR); tt=np.arange(n)/SR
    s=sum(np.sin(2*np.pi*hz(m)*h*tt)/h**bright*np.exp(-tt*(6+h*3)) for h in range(1,6))
    return s*env_adsr(n,0.004,0.05)
for at,m in [(1.05,74),(1.27,78),(1.49,81),(1.71,86)]: add(sfx,pluck(m),at,0.10)
add(sfx,pluck(62,1.2,1.5),3.5,0.12); add(sfx,pluck(74,1.2,2.2),3.52,0.07)
for at,m in [(7.0,78),(7.25,81)]: add(sfx,pluck(m),at,0.08)          # presence
for at,m in [(8.05,86),(8.2,90)]: add(sfx,pluck(m,0.4,2.5),at,0.06)  # vents open
for at,m in [(10.0,69),(11.4,74)]: add(sfx,pluck(m,0.7,1.8),at,0.09) # vents close / at target
add(sfx,pluck(81,0.9,2.0),11.65,0.06)
for at,m in [(13.65,69),(13.95,73),(14.25,76)]: add(sfx,pluck(m),at,0.08)
add(sfx,pluck(62,1.5,1.5),17.1,0.12); add(sfx,pluck(78,1.5,2.2),17.12,0.05)
# soft risers into scene changes (lowpassed noise via FFT)
def riser(dur):
    n=int(dur*SR); w=rng.standard_normal(n)
    F=np.fft.rfft(w); fr=np.fft.rfftfreq(n,1/SR); F*=1/(1+(fr/1800)**2); w=np.fft.irfft(F,n)
    return w/np.abs(w).max()*np.linspace(0,1,n)**2
for at in [3.4,6.6,13.0,17.0]: add(sfx,riser(0.7),at-0.7,0.05)

# shared reverb for "same space"
def reverb(x, rt=1.8, wet=0.22):
    n=int(rt*SR); ir=rng.standard_normal(n)*np.exp(-np.arange(n)/SR*(6.9/rt))
    F=np.fft.rfft(ir); fr=np.fft.rfftfreq(n,1/SR); ir=np.fft.irfft(F/(1+(fr/4000)**2),n)
    L=len(x)+n; y=np.fft.irfft(np.fft.rfft(x,L)*np.fft.rfft(ir,L),L)[:len(x)]
    y/= (np.abs(y).max()+1e-9); return x + wet*y*np.abs(x).max()
mix = reverb(music,2.0,0.25) + reverb(sfx,2.0,0.35)
# gentle low-pass on whole mix to tame harshness
F=np.fft.rfft(mix); fr=np.fft.rfftfreq(N,1/SR); mix=np.fft.irfft(F/np.sqrt(1+(fr/9000)**4),N)
# fades
mix[:int(0.03*SR)]*=np.linspace(0,1,int(0.03*SR))
fo=int(1.2*SR); mix[-fo:]*=np.linspace(1,0,fo)**1.5
mix=np.tanh(mix/np.abs(mix).max()*1.2)/np.tanh(1.2)*0.82
st=np.stack([mix,mix],1)
with wave.open('audio.wav','wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes((st*32767).astype('<i2').tobytes())
print('ok', np.abs(mix).max())
