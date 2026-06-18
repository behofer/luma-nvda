import wave
import struct
import math
import os

def generate_wav(filename, frequency=1000, duration_ms=100, type="sine", volume=0.5):
    """Generiert eine hochwertige 16-bit Mono WAV Datei."""
    sample_rate = 44100
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    
    # Ordner erstellen falls nicht vorhanden
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    
    with wave.open(filename, 'w') as f:
        f.setparams((1, 2, sample_rate, num_samples, 'NONE', 'not compressed'))
        for i in range(num_samples):
            t = i / sample_rate
            # Hüllkurve (Envelope) gegen Knacken am Anfang/Ende
            fade_in = min(1.0, i / (sample_rate * 0.01)) 
            fade_out = min(1.0, (num_samples - i) / (sample_rate * 0.05))
            
            if type == "sine":
                val = math.sin(2.0 * math.pi * frequency * t)
            elif type == "pulse": # Weicherer Puls
                val = 0.5 * math.sin(2.0 * math.pi * frequency * t) + 0.2 * math.sin(4.0 * math.pi * frequency * t)
            
            sample = int(val * 32767 * volume * fade_in * fade_out)
            f.writeframes(struct.pack('<h', sample))

# 1. Das "Ticken" (Minimalistisches Verarbeiten)
generate_wav("assets/process_tick.wav", frequency=1200, duration_ms=30, volume=0.3)

# 2. Der "Thinking Pulse" (Dezentes, tieferes Atmen der KI)
generate_wav("assets/process_pulse.wav", frequency=440, duration_ms=600, type="pulse", volume=0.2)

# 3. Der "Success Jingle" (Kurzer Bestätigungston)
# Hierfür erstellen wir eine kleine aufsteigende Sequenz
def generate_success_jingle(filename):
    sample_rate = 44100
    with wave.open(filename, 'w') as f:
        f.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
        for freq in [523.25, 659.25, 783.99]: # C5, E5, G5
            num_s = int(sample_rate * 0.1)
            for i in range(num_s):
                env = (num_s - i) / num_s
                val = int(math.sin(2.0 * math.pi * freq * i / sample_rate) * 32767 * 0.3 * env)
                f.writeframes(struct.pack('<h', val))

generate_success_jingle("assets/success.wav")

print("Sounds generiert in /assets/")