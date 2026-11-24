"""
FlowCaption: cola um link (YouTube, X/Twitter, Instagram) e recebe o video legendado em portugues.

Uso:
    python legendar.py                 -> abre uma janelinha para colar o link
    python legendar.py <link>          -> roda direto no terminal
    python legendar.py arquivo.mp4     -> legenda um video que voce ja baixou

Resultado na pasta escolhida na janela (padrao ~/Legendados)/<titulo>/: legendado.mp4, legenda_pt.srt e original.mp4
"""
import json
import os
import re
import shutil
import subprocess
import urllib.request
import sys
from pathlib import Path

import contextlib
import threading


class Cancelado(BaseException):
    """O usuario interrompeu este video (nao e um erro; BaseException para nenhum 'except Exception' engolir)."""


class Trabalho:
    """Um video em andamento: guarda o pedido de interrupcao e os processos (ffmpeg...) que ele abriu."""
    def __init__(self):
        self.cancelado = threading.Event()
        self.procs = []

    def cancelar(self):
        self.cancelado.set()
        for p in list(self.procs):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass


CTX = threading.local()   # o Trabalho da thread atual


def checar():
    """Levanta Cancelado se o usuario interrompeu o video que esta sendo processado nesta thread."""
    t = getattr(CTX, "trabalho", None)
    if t is not None and t.cancelado.is_set():
        raise Cancelado()


class _ProcessoRegistrado(subprocess.Popen):
    """Nenhuma ferramenta chamada pelo programa (ffmpeg, ffprobe, PowerShell, e as do yt-dlp) deve abrir janela de
    terminal (Windows), e todo processo aberto por um video fica registrado para poder ser encerrado ao interromper."""
    def __init__(self, *args, **kwargs):
        if sys.platform == "win32":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW
        super().__init__(*args, **kwargs)
        t = getattr(CTX, "trabalho", None)
        if t is not None:
            t.procs[:] = [p for p in t.procs if p.poll() is None] + [self]
            if t.cancelado.is_set():
                self.kill()


subprocess.Popen = _ProcessoRegistrado


class VezEmFila:
    """Uma etapa de cada vez, na ordem de chegada (por exemplo: so um video usa a placa de video por vez,
    e so um baixa por vez; mas um pode baixar enquanto outro e processado)."""
    def __init__(self):
        self.cond = threading.Condition()
        self.espera = []
        self.ocupado = False

    @contextlib.contextmanager
    def __call__(self, aviso=None):
        chave = object()
        with self.cond:
            esperou = self.ocupado or bool(self.espera)
            self.espera.append(chave)
        if esperou and aviso:
            aviso()
        with self.cond:
            try:
                while self.ocupado or self.espera[0] is not chave:
                    self.cond.wait(0.5)
                    checar()
            except BaseException:
                self.espera.remove(chave)
                self.cond.notify_all()
                raise
            self.espera.pop(0)
            self.ocupado = True
        try:
            yield
        finally:
            with self.cond:
                self.ocupado = False
                self.cond.notify_all()


class Posse:
    """A vez numa VezEmFila que pode ser soltada e pega de novo (ex.: solta a placa de video enquanto espera o
    Gemini traduzir, e pega de volta para gravar a legenda)."""
    def __init__(self, vez, aviso=None):
        self.vez, self.aviso, self.ctx = vez, aviso, None

    def pegar(self):
        if self.ctx is None:
            ctx = self.vez(self.aviso)
            ctx.__enter__()
            self.ctx = ctx

    def soltar(self):
        if self.ctx is not None:
            ctx, self.ctx = self.ctx, None
            ctx.__exit__(None, None, None)


VEZ_DOWNLOAD = VezEmFila()   # um download por vez
VEZ_PROC = VezEmFila()       # uma transcricao/codificacao por vez (placa de video)

NOME_APP = "FlowCaption"
VERSAO = "1.0.0"                 # suba este numero a cada versao publicada (tag do GitHub: v1.0.1, v1.1.0...)
REPOSITORIO = "iagobelotti/flowcaption"                 # "usuario/repositorio" no GitHub; vazio = sem verificacao de atualizacao
PASTA_APP = Path(__file__).resolve().parent
if (PASTA_APP / "ffmpeg").is_dir():   # ffmpeg portatil, se houver
    os.environ["PATH"] = str(PASTA_APP / "ffmpeg") + os.pathsep + os.environ.get("PATH", "")
ARQ_CONFIG = PASTA_APP / "config.json"


def ler_config():
    try:
        return json.loads(ARQ_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_config(**kw):
    cfg = ler_config()
    cfg.update(kw)
    ARQ_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


SAIDA_BASE = Path(ler_config().get("pasta_saida") or (Path.home() / "Legendados"))
MODELO_CLAUDE = "claude-sonnet-5-5"
MAX_CHARS_FONTE = 60      # tamanho maximo de cada trecho antes de traduzir
MAX_CHARS_LINHA = 42      # largura de cada linha da legenda
LOTE_TRADUCAO = 40


ENV_LIDO = None


def carregar_env():
    """Le NOME=valor de .env (aceita tambem .env.txt, env, env.txt). Variaveis do Windows tem prioridade."""
    global ENV_LIDO
    arq = next((PASTA_APP / n for n in (".env", ".env.txt", "env", "env.txt") if (PASTA_APP / n).is_file()), None)
    if arq is None:
        return
    ENV_LIDO = arq.name
    for linha in arq.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, valor = linha.split("=", 1)
        nome = nome.removeprefix("export ").strip()
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if nome and valor and not os.environ.get(nome):
            os.environ[nome] = valor


carregar_env()


def arquivo_env():
    """O arquivo onde fica a chave do Gemini (o que ja existe ou, se nao houver, 'env')."""
    return next((PASTA_APP / n for n in (".env", ".env.txt", "env", "env.txt") if (PASTA_APP / n).is_file()),
                PASTA_APP / "env")


def salvar_chave_gemini(chave):
    """Grava GEMINI_API_KEY no arquivo env desta pasta (mantem as outras linhas) e passa a valer ja."""
    chave = chave.strip()
    arq = arquivo_env()
    linhas = arq.read_text(encoding="utf-8-sig").splitlines() if arq.is_file() else []
    linhas = [l for l in linhas if l.strip().removeprefix("export ").split("=", 1)[0].strip() != "GEMINI_API_KEY"]
    linhas.append(f"GEMINI_API_KEY={chave}")
    arq.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = chave
    global ENV_LIDO
    ENV_LIDO = arq.name


def testar_chave_gemini(chave):
    """Pergunta ao Google se a chave vale. Devolve (ok, mensagem)."""
    import urllib.error
    import urllib.request
    chave = chave.strip()
    if len(chave) < 20 or any(c.isspace() for c in chave):
        return False, "Essa chave parece incompleta. Copie a chave inteira (começa com AIza)."
    req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                 headers={"x-goog-api-key": chave})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True, "Chave válida."
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return False, "O Google não aceitou essa chave. Confira se copiou inteira e sem espaços."
        return False, f"O Google respondeu com erro {e.code}. Tente de novo em instantes."
    except Exception as e:  # noqa: BLE001
        return False, "Não consegui falar com o Google (" + str(e)[:80] + "). Confira a internet."


def cookies_do_site(site):
    """O cookies.txt do site ('youtube' ou 'instagram') que esta na pasta do programa, ou None."""
    return achar_cookies(f"https://www.{site}.com/")


# ---------------------------------------------------------------- utilidades
def nome_seguro(txt, limite=60):
    txt = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", txt).strip().strip(".")
    return (txt[:limite].strip() or "video")


def tempo_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_SONDAS = {}   # (caminho, tamanho, data) -> dados do ffprobe; o mesmo arquivo nao e lido de novo


def _sondar(video):
    """Duracao, largura, altura e fps do video (um ffprobe so, guardado enquanto o arquivo nao mudar)."""
    p = Path(video).resolve()
    st = p.stat()
    chave = (str(p), st.st_size, st.st_mtime_ns)
    if chave not in _SONDAS:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate:format=duration", "-of", "json", str(p)],
            capture_output=True, text=True, check=True)
        d = json.loads(r.stdout)
        s = d["streams"][0]
        try:
            a, _, b = str(s.get("avg_frame_rate") or "").partition("/")
            fps = float(a) / float(b or 1)
        except (ValueError, ZeroDivisionError):
            fps = 0
        if len(_SONDAS) > 50:
            _SONDAS.clear()
        _SONDAS[chave] = (float(d["format"]["duration"]), int(s["width"]), int(s["height"]),
                          fps if 1 <= fps <= 240 else 30.0)
    return _SONDAS[chave]


def duracao_e_altura(video):
    return _sondar(video)[:3]


# ------------------------------------------------------------------ trechos
def ler_tempo(txt):
    """'1:12:40', '72:40', '4360', '1h12m40s', '12m' -> segundos (float). None se vazio/invalido."""
    txt = (txt or "").strip().lower().replace(",", ".")
    if not txt:
        return None
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m(?:in)?)?\s*(?:(\d+(?:\.\d+)?)s?)?", txt)
    if m and any(m.groups()) and re.search(r"[hms]", txt):
        h, mi, se = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    partes = txt.split(":")
    try:
        nums = [float(x) for x in partes]
    except ValueError:
        return None
    if len(nums) > 3 or any(n < 0 for n in nums):
        return None
    t = 0.0
    for n in nums:
        t = t * 60 + n
    return t


def mostrar_tempo(seg):
    seg = int(round(seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def tempo_no_nome(seg):
    seg = int(round(seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return (f"{h}h" if h else "") + f"{m:02d}m{s:02d}s"


def link_no_tempo(url, seg):
    """Link do YouTube que abre direto no segundo pedido (outros sites: o link como esta)."""
    if not re.search(r"youtube\.com|youtu\.be", url or ""):
        return url
    url = re.sub(r"([?&])t=[^&]*&?", r"\1", url).rstrip("?&")
    return f"{url}{'&' if '?' in url else '?'}t={int(seg)}s"


def cortar(origem, destino, ini, fim, log=print):
    """Recorta [ini, fim] recodificando (corte exato e ja no formato que o Premiere le bem)."""
    g = str(_gop(origem))
    base = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ini:.3f}", "-i", str(origem), "-t", f"{fim - ini:.3f}",
            "-map", "0:v:0", "-map", "0:a?", "-pix_fmt", "yuv420p", "-g", g, "-bf", "0"]
    fim_cmd = ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(destino)]
    tentativas = []
    if _tem_nvenc():
        tentativas.append(base + NV_ORIGINAL + fim_cmd)
    tentativas.append(base + X264_ORIGINAL + fim_cmd)
    erro = ""
    for cmd in tentativas:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and Path(destino).exists() and Path(destino).stat().st_size > 0:
            return
        erro = r.stderr.strip()[-200:]
    raise RuntimeError("nao consegui recortar o trecho: " + erro)


# ---------------------------------------------------------- codificacao (H.264)
LIMITE = ["-maxrate", "6M", "-bufsize", "6M"]   # no maximo 6 Mbps (video simples fica abaixo disso)
NV_LEGENDADO = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "22", "-b:v", "5M"] + LIMITE
NV_ORIGINAL = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21", "-b:v", "5M"] + LIMITE
X264_LEGENDADO = ["-c:v", "libx264", "-preset", "medium", "-crf", "19"] + LIMITE
X264_ORIGINAL = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"] + LIMITE


# ------------------------------------------------------------- achar trecho por assunto
def _legendas_do_youtube(info, log):
    """Pega as legendas (manuais ou automaticas) que o proprio site oferece, sem baixar o video.
    Devolve [(segundo, texto)] ou []."""
    import yt_dlp
    idioma_video = (info.get("language") or "").split("-")[0]
    fontes, vistos = [], set()
    for chave in ("subtitles", "automatic_captions"):
        subs = {k: v for k, v in (info.get(chave) or {}).items() if k != "live_chat"}
        ordem = ([k for k in subs if k.endswith("-orig")] if chave == "automatic_captions" else []) + \
            [idioma_video, "pt-BR", "pt", "en", "en-US"] + (list(subs) if chave == "subtitles" else [])
        for l in ordem:
            if l and l in subs and (chave, l) not in vistos:
                vistos.add((chave, l))
                fontes.append((chave, l, subs[l]))
    for chave, lang, formatos in fontes[:4]:
        fmt = next((f for f in formatos if f.get("ext") == "json3"), None)
        if not fmt:
            continue
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **YTDLP_EJS}) as ydl:
                dados = json.loads(ydl.urlopen(fmt["url"]).read().decode("utf-8"))
            linhas = []
            for ev in dados.get("events", []):
                txt = "".join(sg.get("utf8", "") for sg in ev.get("segs") or []).replace("\n", " ").strip()
                if txt:
                    linhas.append((ev.get("tStartMs", 0) / 1000, txt))
            if linhas:
                log(f"  usando as legendas {'automaticas' if chave == 'automatic_captions' else 'do video'} ({lang})")
                return linhas
        except Exception as e:  # noqa: BLE001
            log(f"  legenda {lang} falhou ({str(e)[:80]})")
    return []


def _transcricao_do_audio(url, log):
    """Sem legendas no site: baixa so o audio e transcreve com o Whisper."""
    import shutil
    import tempfile
    import yt_dlp
    tmp = Path(tempfile.mkdtemp(prefix="fc_audio_"))
    try:
        log("  esse video nao tem legendas: baixando so o audio para transcrever (mais lento)...")
        opcoes = {"outtmpl": str(tmp / "audio.%(ext)s"), "format": "ba/b", "quiet": True, "no_warnings": True,
                  "noplaylist": True}
        cookies = achar_cookies(url)
        if cookies:
            opcoes["cookiefile"] = str(cookies)
        with yt_dlp.YoutubeDL({**opcoes, **YTDLP_EJS}) as ydl:
            ydl.extract_info(url, download=True)
        arq = next(tmp.glob("audio.*"))
        segs, _ = transcrever(arq, log)
        return [(sg.start, sg.text.strip()) for sg in segs if sg.text.strip()]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


PROMPT_ACHAR = (
    "Abaixo esta a transcricao de um video, com o tempo em segundos no inicio de cada linha. "
    "O usuario quer os trechos em que: \"{assunto}\". Encontre TODAS as passagens em que isso aparece (ate {maximo}), "
    "cada uma separada, sem juntar passagens distantes. Para cada uma, de o inicio e o fim em segundos (comece um pouco antes do assunto aparecer e termine "
    "quando a fala sobre ele acabar, sem cortar frase no meio) e um resumo de uma frase em portugues do Brasil. "
    "Se nada corresponder, devolva lista vazia."
)


def _gemini_com_paciencia(modelo, corpo, log, tentativas=3):
    """Chama o Gemini; se ele estiver sobrecarregado (503/500) ou limitando (429), espera e tenta de novo."""
    import time
    for k in range(tentativas):
        try:
            return _gemini(f"/v1beta/models/{modelo}:generateContent", corpo)
        except ErroGemini as e:
            if e.codigo == 400 and "thinkingConfig" in corpo.get("generationConfig", {}):
                corpo["generationConfig"].pop("thinkingConfig", None)   # modelo nao aceita a opcao
                continue
            if e.codigo in (429, 500, 503) and not e.diario and k < tentativas - 1:
                espera = min(e.espera or (4 * (k + 1)), 30)
                log(f"  Gemini ocupado ({e.codigo}); tentando de novo em {int(espera)} s...")
                time.sleep(espera)
                continue
            raise
    return _gemini(f"/v1beta/models/{modelo}:generateContent", corpo)


def _arq_transcricao(url):
    import hashlib
    return PASTA_CACHE / f"trechos_{hashlib.sha1(url.strip().encode('utf-8')).hexdigest()[:16]}.json"


def carregar_transcricao(url, log, forcar=False):
    """Le UMA vez as informacoes e a fala do video (legendas do site ou, se nao houver, o audio transcrito).
    Fica guardada em cache (a mesma live nao e lida de novo). Devolve {'url','titulo','duracao','linhas':[[s, texto]]}."""
    import yt_dlp
    arq = _arq_transcricao(url)
    if not forcar:
        try:
            d = json.loads(arq.read_text(encoding="utf-8"))
            if d.get("linhas"):
                log("Fala do video ja estava guardada.")
                return d
        except (OSError, ValueError):
            pass
    log("Lendo as informacoes do video...")
    opcoes = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    cookies = achar_cookies(url)
    if cookies:
        opcoes["cookiefile"] = str(cookies)
    log(f"  cookies: {cookies.name}" if cookies else "  sem cookies.txt (login)")
    try:
        with yt_dlp.YoutubeDL({**opcoes, **YTDLP_EJS}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "members" in msg or "sign in" in msg or "log in" in msg or "login" in msg:
            if cookies:
                raise RuntimeError("O YouTube nao reconheceu o login do cookies.txt (ele costuma vencer depois de um "
                                   "tempo). Exporte o cookies.txt de novo, com o YouTube aberto e logado, e tente "
                                   "outra vez.") from None
            raise RuntimeError("Esse video exige login (e so para membros ou com restricao). Exporte o cookies.txt do "
                               "YouTube e deixe na pasta do programa.") from None
        raise
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [info])[0] or {}
    duracao = float(info.get("duration") or 0)
    linhas = _legendas_do_youtube(info, log) or _transcricao_do_audio(url, log)
    if not linhas:
        raise RuntimeError("Nao encontrei fala nesse video.")
    d = {"url": url.strip(), "titulo": " ".join(str(info.get("title") or "").split()), "duracao": duracao,
         "linhas": [[float(t), x] for t, x in linhas]}
    try:
        PASTA_CACHE.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return d


_BUSCAS = {}   # (link, assunto, maximo) -> resultado: repetir a mesma busca e instantaneo
JANELA_BUSCA = 2700   # segundos de fala por pedido ao Gemini: lives longas sao procuradas por partes


def buscar_assunto(dados, assunto, log, maximo=8):
    """Procura o assunto na fala ja carregada. Videos longos sao divididos em partes de ~45 min (uma chamada por
    parte; `maximo` vale para cada parte), assim nada fica de fora. Devolve na ordem do video:
    [{'inicio', 'fim', 'resumo', 'fala'}]."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise RuntimeError("A busca por assunto usa o Gemini, e nao achei a GEMINI_API_KEY.")
    duracao, linhas = float(dados.get("duracao") or 0), [(t, x) for t, x in dados["linhas"]]
    # junta em blocos de ~15 s: menos linhas = menos tokens, e a precisao continua boa
    blocos, atual, ini = [], [], None
    for t, txt in linhas:
        if ini is None:
            ini = t
        if t - ini >= 15 and atual:
            blocos.append((ini, " ".join(atual)))
            atual, ini = [], t
        atual.append(txt)
    if atual:
        blocos.append((ini, " ".join(atual)))
    partes, cur, t0 = [], [], None
    for b in blocos:
        if t0 is None:
            t0 = b[0]
        if b[0] - t0 >= JANELA_BUSCA and cur:
            partes.append(cur)
            cur, t0 = [], b[0]
        cur.append(b)
    if cur:
        partes.append(cur)
    esquema = {"type": "OBJECT", "required": ["trechos"], "properties": {"trechos": {"type": "ARRAY", "items": {
        "type": "OBJECT", "required": ["inicio", "fim", "resumo"], "properties": {
            "inicio": {"type": "NUMBER"}, "fim": {"type": "NUMBER"}, "resumo": {"type": "STRING"}}}}}}
    modelos = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else [GEMINI_PADRAO, *GEMINI_ALTERNATIVOS]
    chave_cache = (dados.get("url"), assunto.strip().lower(), maximo)
    if chave_cache[0] and chave_cache in _BUSCAS:
        log("Essa busca ja foi feita: mostrando o resultado guardado.")
        return [dict(a) for a in _BUSCAS[chave_cache]]
    trab = getattr(CTX, "trabalho", None)
    estado = {"prontas": 0}
    trava = threading.Lock()

    def uma_parte(n, parte):
        CTX.trabalho = trab   # o botao Parar tambem vale nas threads
        checar()
        texto = "\n".join(str(int(t)) + "|" + re.sub(r"\s+", " ", x) for t, x in parte)
        ultimo = None
        for modelo in modelos:
            corpo = {"systemInstruction": {"parts": [{"text": PROMPT_ACHAR.format(assunto=assunto, maximo=maximo)}]},
                     "contents": [{"role": "user", "parts": [{"text": texto}]}],
                     "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                                          "responseSchema": esquema, "thinkingConfig": {"thinkingBudget": 0}}}
            try:
                r = _gemini_com_paciencia(modelo, corpo, lambda m: None if trab is not None and trab.cancelado.is_set()
                                          else log(m))
            except ErroGemini as e:
                ultimo = e
                if e.codigo in (404, 429, 500, 503) and not e.diario:   # tenta o proximo modelo
                    continue
                raise
            achados = json.loads(r["candidates"][0]["content"]["parts"][0]["text"]).get("trechos") or []
            parte_saida = []
            for a in achados:
                ini, fim = float(a.get("inicio", 0)), float(a.get("fim", 0))
                if duracao:
                    ini, fim = max(0.0, min(ini, duracao)), max(0.0, min(fim, duracao))
                if fim - ini < 2:
                    continue
                fala = " ".join(x for t, x in linhas if ini <= t <= ini + 20)[:220]
                parte_saida.append({"inicio": ini, "fim": fim, "resumo": str(a.get("resumo", "")).strip(), "fala": fala})
            with trava:
                estado["prontas"] += 1
                if len(partes) > 1:
                    log(f"Procurando \"{assunto}\"... {estado['prontas']} de {len(partes)} partes prontas")
            return parte_saida
        raise ultimo or RuntimeError("o Gemini nao respondeu")

    log(f"Procurando \"{assunto}\" com o Gemini ({len(partes)} parte(s), varias ao mesmo tempo)...")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(5, len(partes))) as pool:
        futuros = [pool.submit(uma_parte, n, p) for n, p in enumerate(partes, 1)]
        saida = []
        for f in futuros:
            saida += f.result()   # na ordem do video; um erro de qualquer parte aparece aqui
    saida.sort(key=lambda a: a["inicio"])
    unicos = []
    for a in saida:   # partes vizinhas podem achar o mesmo trecho
        if unicos and a["inicio"] < unicos[-1]["fim"]:
            continue
        unicos.append(a)
    if chave_cache[0]:
        _BUSCAS[chave_cache] = [dict(a) for a in unicos]
    return unicos


def encontrar_trecho(url, assunto, log):
    """Devolve (duracao_do_video, [{'inicio', 'fim', 'resumo', 'fala'}]) usando as legendas + Gemini."""
    dados = carregar_transcricao(url, log)
    return dados["duracao"], buscar_assunto(dados, assunto, log)


# ------------------------------------------------------------------ download
FORMATO = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b"
FORMATO_AUDIO = "ba/b"


def formato_padrao(ctx):
    """Escolha de formato padrao: 1080p; se o video nao tem 1080p, a menor qualidade ACIMA dela (ex.: 1440p);
    se so tem abaixo, a melhor delas. Prefere H.264 + AAC (m4a)."""
    formatos = ctx.get("formats") or []
    so_video = [f for f in formatos if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
                and f.get("height")]
    juntos = [f for f in formatos if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
              and f.get("height")]
    so_audio = [f for f in formatos if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    alturas = {f["height"] for f in so_video + juntos}
    if not alturas:
        if formatos:
            yield formatos[-1]
        return
    acima = [h for h in alturas if h > 1080]
    alvo = 1080 if 1080 in alturas else (min(acima) if acima else max(alturas))

    def nota(f):
        return (str(f.get("vcodec", "")).startswith("avc1"), f.get("fps") or 0, f.get("tbr") or 0)
    videos = [f for f in so_video if f["height"] == alvo]
    if videos and so_audio:
        v = max(videos, key=nota)
        m4a = [f for f in so_audio if f.get("ext") == "m4a"]
        pool = m4a if (m4a and str(v.get("vcodec", "")).startswith("avc1")) else so_audio
        au = max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        yield {"format_id": f"{v['format_id']}+{au['format_id']}", "ext": "mp4" if v.get("ext") == "mp4" else v.get("ext"),
               "requested_formats": [v, au], "protocol": f"{v.get('protocol')}+{au.get('protocol')}"}
        return
    pool = [f for f in juntos if f["height"] == alvo] or videos
    if pool:
        yield max(pool, key=nota)
    else:
        yield formatos[-1]


def formato_ate(altura):
    """Formato do yt-dlp para baixar no maximo `altura` pixels (None = o padrao do programa, ver formato_padrao)."""
    if not altura:
        return formato_padrao
    a = int(altura)
    return f"bv*[height<={a}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={a}]+ba/b[height<={a}]/b"


def listar_qualidades(url, log=print):
    """Pergunta ao site quais qualidades o video tem (sem baixar nada).
    Devolve {"titulo", "duracao", "ao_vivo", "opcoes": [{"altura", "fps", "bytes"}...]} da maior para a menor."""
    import yt_dlp
    opcoes = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20}
    ck = achar_cookies(url)
    if ck:
        opcoes["cookiefile"] = str(ck)
    with yt_dlp.YoutubeDL({**opcoes, **YTDLP_EJS}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [info])[0] or {}
    dur = float(info.get("duration") or 0)

    def tamanho(f):
        b = f.get("filesize") or f.get("filesize_approx")
        if not b and f.get("tbr") and dur:
            b = f["tbr"] * 1000 / 8 * dur
        return int(b) if b else 0
    formatos = info.get("formats") or []
    audios = [f for f in formatos if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    audio_b = max((tamanho(f) for f in audios), default=0)
    por_altura = {}
    for f in formatos:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        melhor = por_altura.get(h)
        chave = (str(f.get("vcodec", "")).startswith("avc1"), f.get("fps") or 0, f.get("tbr") or 0)
        if melhor is None or chave > melhor[0]:
            por_altura[h] = (chave, f)
    saida = []
    for h, (_, f) in sorted(por_altura.items(), reverse=True):
        b = tamanho(f)
        saida.append({"altura": int(h), "fps": int(round(f.get("fps") or 0)),
                      "bytes": (b + (0 if f.get("acodec") not in (None, "none") else audio_b)) if b else 0})
    return {"titulo": info.get("title") or "", "duracao": dur, "ao_vivo": bool(info.get("is_live")), "opcoes": saida}
# O YouTube exige resolver um desafio em JavaScript (deno/node); o script que faz isso o yt-dlp baixa do GitHub oficial dele.
YTDLP_EJS = {"remote_components": ["ejs:github"]}
MP3 = ["-c:a", "libmp3lame", "-b:a", "192k"]   # a fonte ja e AAC/Opus de ~128 kbps: 192k nao perde nada


def _duracao_audio(arq):
    """Duracao (s) de um arquivo, com ou sem video."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arq)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extrair_mp3(origem, destino, trecho=None):
    """Grava o audio de um arquivo (video ou audio) em MP3. trecho=[inicio, fim] em segundos (None = tudo)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if trecho:
        cmd += ["-ss", f"{trecho[0]:.3f}"]
    cmd += ["-i", str(origem)]
    if trecho:
        cmd += ["-t", f"{trecho[1] - trecho[0]:.3f}"]
    cmd += ["-vn", "-map", "0:a:0"] + MP3 + [str(destino)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    checar()
    if r.returncode != 0 or not Path(destino).exists() or Path(destino).stat().st_size == 0:
        raise RuntimeError("nao consegui gravar o MP3: " + (r.stderr.strip()[-200:] or "o video nao tem audio?"))
    return Path(destino)


def _meta(info, url):
    info = info or {}
    chave = (info.get("extractor_key") or info.get("extractor") or "").lower()
    fonte = ("youtube" if "youtube" in chave else "x" if "twitter" in chave or "x.com" in url
             else "instagram" if "instagram" in chave else "outro")
    return {"fonte": fonte, "titulo": info.get("title") or "", "descricao": info.get("description") or "",
            "perfil": info.get("uploader_id") or info.get("channel") or info.get("uploader") or "",
            "id": str(info.get("id") or "")}


def _apagar_com_calma(*arqs):
    """Apaga arquivos; se o Windows ainda estiver com o arquivo preso (WinError 32), tenta de novo por alguns segundos."""
    import time
    for a in arqs:
        for _ in range(20):
            try:
                Path(a).unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)


def baixar_trecho_direto(url, pasta, log, trecho, cookies=None, sem_avanco=90, audio=False, altura=None):
    """Baixa SO o trecho: o yt-dlp so descobre os enderecos do video e do audio; o ffmpeg pula direto para o
    inicio (pedindo so esse pedaco ao servidor) e ja grava no formato do Premiere (H.264, keyframe a cada 1 s,
    sem B-frames). Mostra o andamento e desiste se ficar `sem_avanco` segundos parado."""
    import time
    import yt_dlp
    ini, fim = trecho
    dur = fim - ini
    log("  descobrindo o endereco do video...")
    opcoes = {"format": FORMATO_AUDIO if audio else formato_ate(altura), "quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies:
        opcoes["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL({**opcoes, **YTDLP_EJS}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [info])[0] or {}
    formatos = info.get("requested_formats") or [info]
    if not all(f.get("url") for f in formatos):
        raise RuntimeError("o site nao informou o endereco do video")
    if audio:
        formatos = formatos[-1:]   # so o endereco do audio
    tem_audio_separado = len(formatos) > 1
    mapas = (["-vn", "-map", "0:a:0"] if audio else
             ["-map", "0:v:0", "-map", "1:a:0"] if tem_audio_separado else ["-map", "0:v:0", "-map", "0:a?"])
    fps = 30.0
    try:
        fps = float(formatos[0].get("fps") or 30)
    except (TypeError, ValueError):
        pass
    g = str(max(1, round(fps)))
    saida = pasta / ("original.mp3" if audio else "original.mp4")
    encoders = [MP3] if audio else ([NV_ORIGINAL] if _tem_nvenc() else []) + [X264_ORIGINAL]
    erro = ""
    for enc in encoders:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
        for f in formatos:   # uma entrada para o video e outra para o audio, cada uma ja no inicio do trecho
            cab = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
            cmd += ["-reconnect", "1", "-reconnect_delay_max", "5"] + (["-headers", cab] if cab else [])
            cmd += ["-ss", f"{ini:.3f}", "-i", f["url"]]
        cmd += ["-t", f"{dur:.3f}"] + mapas + enc
        if not audio:
            cmd += ["-pix_fmt", "yuv420p", "-g", g, "-bf", "0", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"]
        cmd += [str(saida)]
        log(f"  baixando so o trecho ({mostrar_tempo(dur)})...")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        estado = {"ultimo": time.time(), "feito": 0.0}

        def ler():
            for linha in p.stdout:
                if linha.startswith("out_time_us=") or linha.startswith("out_time_ms="):
                    try:
                        t = int(linha.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    if t > estado["feito"]:
                        estado["feito"] = t
                        estado["ultimo"] = time.time()
        th = threading.Thread(target=ler, daemon=True)
        th.start()
        err_buf = []

        def ler_erro():   # esvazia o stderr: se o buffer encher, o ffmpeg trava
            for linha in p.stderr:
                err_buf.append(linha)
                del err_buf[:-40]
        th2 = threading.Thread(target=ler_erro, daemon=True)
        th2.start()
        ultimo_pct = -10
        while p.poll() is None:
            time.sleep(1)
            checar()
            pct = int(min(100, estado["feito"] * 100 / max(dur, 1)))
            if pct >= ultimo_pct + 5:
                ultimo_pct = pct
                log(f"Baixando o trecho... {pct}%")
            if time.time() - estado["ultimo"] > sem_avanco:
                p.kill()
                try:
                    p.wait(timeout=15)   # espera o ffmpeg soltar o arquivo (senao o Windows da WinError 32)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"o download ficou {sem_avanco} s sem avancar")
        th.join(timeout=2)
        th2.join(timeout=2)
        checar()
        if p.returncode == 0 and saida.exists() and saida.stat().st_size > 0:
            return saida, _meta(info, url)
        erro = "".join(err_buf).strip()[-200:]
        _apagar_com_calma(saida)
    raise RuntimeError("ffmpeg falhou: " + erro)


def achar_cookies(url=""):
    """Cookies exportados pela extensao 'Get cookies.txt LOCALLY', do site certo:
    Instagram -> www.instagram.com_cookies.txt; YouTube -> www.youtube.com_cookies.txt ou yt-cookies.txt.
    (cookies.txt generico vale para qualquer site.) Procura na pasta do programa, na de cima, em Downloads e na
    pasta do usuario."""
    url = (url or "").lower()
    pastas = [PASTA_APP, PASTA_APP.parent, Path.home() / "Downloads", Path.home()]
    if "instagram.com" in url:
        padroes = ["*instagram.com_cookies*.txt"]
    elif "youtube.com" in url or "youtu.be" in url:
        padroes = ["*youtube.com_cookies*.txt", "yt-cookies*.txt"]
    else:
        padroes = []
    padroes += ["cookies.txt", "cookies.txt.txt"]
    for padrao in padroes:
        candidatos = []
        for pasta in pastas:
            try:
                candidatos += [p for p in pasta.glob(padrao) if p.is_file()]
            except OSError:
                pass
        if candidatos:
            return max(candidatos, key=lambda p: p.stat().st_mtime)
    return None


def nomes_cookies(arq):
    """So os NOMES dos cookies (nunca os valores), para diagnostico."""
    nomes = []
    for linha in arq.read_text(encoding="utf-8", errors="replace").splitlines():
        if linha.startswith("#HttpOnly_"):
            linha = linha[len("#HttpOnly_"):]
        partes = linha.split("\t")
        if len(partes) >= 7 and not linha.startswith("#"):
            nomes.append(partes[5])
    return nomes


IG_BASE = os.environ.get("IG_BASE", "https://www.instagram.com").rstrip("/")
IG_APP_ID = "936619743392459"   # app id do site web do Instagram


def _blobs_json(html):
    """Os dados que o Instagram embute na pagina (<script type="application/json">)."""
    for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _itens_com_video(o, achados):
    if isinstance(o, dict):
        if isinstance(o.get("video_versions"), list) and o["video_versions"]:
            achados.append(o)
        for v in o.values():
            _itens_com_video(v, achados)
    elif isinstance(o, list):
        for v in o:
            _itens_com_video(v, achados)


def baixar_instagram_pela_pagina(url, pasta, arq_cookies, log):
    """Faz o que o site do Instagram faz: abre a pagina (com o seu login), le os dados embutidos nela
    e baixa o video direto do servidor de arquivos (cdninstagram), com audio junto.
    Serve para stories (/stories/usuario/ID), reels (/reel/CODIGO) e posts (/p/CODIGO)."""
    import http.cookiejar
    m_story = re.search(r"instagram\.com/stories/([^/?#]+)/(\d+)", url)
    m_code = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)", url)
    if not (m_story or m_code):
        raise RuntimeError("link do Instagram nao reconhecido")

    jar = http.cookiejar.MozillaCookieJar(str(arq_cookies))
    jar.load(ignore_discard=True, ignore_expires=True)
    try:
        from curl_cffi import requests as http   # se passa por um Chrome de verdade
        sess = http.Session(impersonate="chrome")
    except ImportError:
        import requests as http
        sess = http.Session()
        sess.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
    for c in jar:
        sess.cookies.set(c.name, c.value, domain=c.domain, path=c.path or "/")

    caminho = re.sub(r"^https?://(www\.)?instagram\.com", "", url.split("?")[0])
    log("  lendo a pagina do Instagram com o seu login...")
    r = sess.get(IG_BASE + caminho, headers={"Accept": "text/html"}, timeout=30)
    html = r.text or ""
    if r.status_code != 200 or "not-logged-in" in html:
        raise RuntimeError(f"pagina: HTTP {r.status_code}, logado: {'NAO' if 'not-logged-in' in html else 'sim'}")

    itens = []
    for blob in _blobs_json(html):
        _itens_com_video(blob, itens)
    if m_story:
        alvo = m_story.group(2)
        item = next((i for i in itens if str(i.get("pk")) == alvo or str(i.get("id", "")).startswith(alvo + "_")), None)
    else:
        alvo = m_code.group(1)
        item = next((i for i in itens if i.get("code") == alvo), None) or (itens[0] if itens else None)
    if item is None:
        raise RuntimeError(f"a pagina abriu, mas nao achei o video (itens com video na pagina: {len(itens)}). "
                           "Se for story, ele pode ter expirado ou ser uma foto.")

    # escolhe a melhor versao: pela resolucao se vier, senao pelo tamanho do arquivo
    def tamanho(v):
        if v.get("height"):
            return (v.get("height") or 0) * (v.get("width") or 0)
        try:
            cr = sess.get(v["url"], headers={"Range": "bytes=0-0"}, timeout=30).headers.get("Content-Range", "")
            return int(cr.rsplit("/", 1)[1])
        except Exception:  # noqa: BLE001
            return 0
    melhor = max(item["video_versions"], key=tamanho)
    log("  baixando o video direto do Instagram...")
    rv = sess.get(melhor["url"], timeout=300)
    if rv.status_code not in (200, 206) or len(rv.content) < 1000:
        raise RuntimeError(f"arquivo do video: HTTP {rv.status_code}")
    destino = pasta / "original.mp4"
    destino.write_bytes(rv.content)
    dono = ((item.get("user") or {}).get("username")) or (m_story.group(1) if m_story else "instagram")
    legenda_post = ((item.get("caption") or {}).get("text")) or ""
    return destino, {"fonte": "instagram", "titulo": "", "descricao": legenda_post, "perfil": dono, "id": alvo}


def _diagnosticar_youtube(url, cookies, log):
    """O YouTube aceitou o login mas nao liberou nenhum formato: anota no log o que o yt-dlp enxergou, para
    descobrir o motivo (falta de executavel de JavaScript, video ainda processando, exigencia de token...)."""
    import shutil
    import yt_dlp
    avisos = []

    class Registro:
        def debug(self, msg):
            pass

        def info(self, msg):
            pass

        def warning(self, msg):
            avisos.append(str(msg))

        def error(self, msg):
            avisos.append(str(msg))
    opcoes = {"quiet": True, "logger": Registro(), "noplaylist": True, "skip_download": True,
              "ignore_no_formats_error": True}
    if cookies:
        opcoes["cookiefile"] = str(cookies)
    js = [n for n in ("deno", "node", "bun", "qjs") if shutil.which(n)]
    log(f"  diagnostico: yt-dlp {yt_dlp.version.__version__}; executavel de JavaScript: {', '.join(js) or 'NENHUM'}")
    try:
        with yt_dlp.YoutubeDL({**opcoes, **YTDLP_EJS}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:  # noqa: BLE001
        log(f"  diagnostico: nao consegui ler o video ({str(e)[:150]})")
        info = {}
    formatos = info.get("formats") or []
    log(f"  diagnostico: estado={info.get('live_status')}, acesso={info.get('availability')}, "
        f"{len(formatos)} formatos listados")
    for f in formatos[:12]:
        log(f"    {f.get('format_id')} {f.get('ext')} {f.get('resolution') or f.get('format_note') or ''} "
            f"{f.get('protocol') or ''} {str(f.get('vcodec') or '')[:5]}")
    for a in avisos[:6]:
        log("    yt-dlp: " + " ".join(a.split())[:220])


def baixar(url, pasta, log, trecho=None, audio=False, altura=None):
    """audio=True: so o som, ja em MP3 (original.mp3)."""
    import yt_dlp
    base = {
        "outtmpl": str(pasta / "original.%(ext)s"),
        "format": formato_ate(altura),   # padrao: ate 1080p, H.264 se existir
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noplaylist": True,
    }
    if audio:
        base.pop("merge_output_format")
        base["format"] = FORMATO_AUDIO
        base["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        base["postprocessor_hooks"] = [lambda d: (checar(), log("Convertendo para MP3...")) if
                                       d.get("status") == "started" and d.get("postprocessor") == "ExtractAudio" else None]
    ultimo_pct = {"v": -10}

    def andamento(d):   # mostra a porcentagem na lista (a cada 5%)
        checar()
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            pct = int(d.get("downloaded_bytes", 0) * 100 / total)
            if pct >= ultimo_pct["v"] + 5:
                ultimo_pct["v"] = pct
                log(f"Baixando... {pct}%")
    base["progress_hooks"] = [andamento]
    if trecho and "instagram.com" not in url:
        for tentativa in (1, 2):   # se travar, tenta de novo com enderecos novos antes de baixar tudo
            try:
                return baixar_trecho_direto(url, pasta, log, trecho, achar_cookies(url), audio=audio, altura=altura)
            except Exception as e:  # noqa: BLE001
                checar()
                _apagar_com_calma(*pasta.glob("original*"))
                if tentativa == 1:
                    log(f"  o trecho travou ({str(e)[:100]}); tentando de novo com enderecos novos...")
                else:
                    log(f"  baixar so o trecho falhou ({str(e)[:150]}); baixando o video inteiro para recortar...")
    # 1) cookies.txt exportado do navegador (funciona com Chrome/Edge), 2) sem login, 3) Firefox.
    # Chrome/Edge direto nao funcionam no Windows: os cookies sao protegidos contra leitura externa.
    arq_cookies = achar_cookies(url)
    if arq_cookies and "instagram.com" in url:
        try:
            return baixar_instagram_pela_pagina(url, pasta, arq_cookies, log)
        except Exception as e:  # noqa: BLE001
            log(f"  leitor da pagina falhou ({str(e)[:120]}); tentando o yt-dlp...")
    tentativas = []
    if arq_cookies:
        nomes = nomes_cookies(arq_cookies)
        log(f"  cookies: {arq_cookies.name} ({len(nomes)} cookies; sessao do Instagram: "
            f"{'SIM' if 'sessionid' in nomes else 'NAO'})")
        if "instagram" in url and "sessionid" not in nomes:
            log("  AVISO: o arquivo nao tem o login. Exporte de novo com o instagram.com aberto e LOGADO.")
        tentativas.append(("com o seu login (cookies.txt)", {"cookiefile": str(arq_cookies)}))
    tentativas.append(("sem login", {}))
    tentativas.append(("com o login do Firefox", {"cookiesfrombrowser": ("firefox",)}))
    erros, corpos = [], []   # corpos: so o texto do erro (sem o nome da tentativa)
    for nome, extra in tentativas:
        try:
            log(f"  tentando {nome}...")
            with yt_dlp.YoutubeDL({**base, **extra, **YTDLP_EJS}) as ydl:
                info = ydl.extract_info(url, download=True)
            if info and info.get("_type") == "playlist":   # story/carrossel com varios itens: pega o primeiro
                info = (info.get("entries") or [info])[0]
            ext = ".mp3" if audio else ".mp4"
            arq = next(pasta.glob("original*" + ext), None) or next(pasta.glob("original*"), None)
            if arq is None:
                raise RuntimeError("download terminou sem arquivo")
            if arq.name != "original" + ext and (not audio or arq.suffix.lower() == ".mp3"):
                arq = arq.rename(pasta / ("original" + ext))
            return arq, _meta(info, url)
        except Exception as e:  # noqa: BLE001
            checar()
            corpos.append(str(e).lower())
            erros.append(f"{nome}: {str(e).replace('ERROR: ', '')[:150]}")
    pede_login = lambda c: any(t in c for t in ("log in", "login", "sign in", "members", "membros"))   # noqa: E731
    youtube = bool(re.search(r"youtube\.com|youtu\.be", url))
    sem_formato = bool(arq_cookies) and "requested format is not available" in (corpos[0] if corpos else "")
    if sem_formato and youtube:
        try:
            _diagnosticar_youtube(url, arq_cookies, log)
        except Exception as e:  # noqa: BLE001
            checar()
            log(f"  diagnostico falhou ({str(e)[:100]})")
    if sem_formato:
        dica = ("\nO YouTube aceitou o seu login, mas nao liberou o video para download. O motivo foi anotado no "
                "flowcaptions.log (linhas 'diagnostico'): me mande esse log.")
    elif not arq_cookies and any(pede_login(c) for c in corpos):
        dica = ("\nEsse conteudo exige login. Exporte o cookies.txt do site (veja o LEIA-ME) e coloque na pasta "
                "do programa.")
    elif arq_cookies and corpos and pede_login(corpos[0]):
        dica = "\nO cookies.txt pode ter expirado: exporte de novo."
    else:
        dica = ""
    raise RuntimeError("Nao consegui baixar o video.\n- " + "\n- ".join(erros) + dica)


# ------------------------------------------------------------- transcricao
def preparar_cuda():
    """Faz o Windows achar as DLLs da NVIDIA instaladas via pip (cuBLAS e cuDNN), se existirem."""
    if sys.platform != "win32":
        return
    for nome in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc"):
        try:
            mod = __import__(nome, fromlist=["x"])
            pasta = os.path.join(list(mod.__path__)[0], "bin")
            if os.path.isdir(pasta):
                os.add_dll_directory(pasta)
                os.environ["PATH"] = pasta + os.pathsep + os.environ.get("PATH", "")
        except Exception:  # noqa: BLE001
            pass


PALAVRAS_COMUNS = {"The", "This", "That", "These", "Those", "And", "But", "For", "With", "From", "His", "Her", "Our",
                   "Their", "What", "When", "Why", "How", "Who", "You", "Your", "Part", "Full", "Video", "New", "Watch"}


def glossario(contexto, limite=220):
    """Nomes proprios do titulo/descricao (palavras com inicial maiuscula), para o Whisper acertar a grafia."""
    limpo = re.sub(r"https?://\S+|[#@]\w+", " ", contexto or "")
    limpo = re.sub(r"['’]s\b", "", limpo)   # Merab's -> Merab
    cap = r"[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\w\-]+(?![\w'’])"   # sem contracoes (I'm, They're)
    nomes = []
    for m in re.finditer(cap + r"(?:\s+" + cap + r")*", limpo):
        palavras = [p for p in m.group(0).split() if p not in PALAVRAS_COMUNS]
        nome = " ".join(palavras).strip()
        antes = limpo[:m.start()].rstrip()
        inicio_de_frase = not antes or antes[-1] in ".!?:;\n\"“(-–—|"
        if len(palavras) == 1 and inicio_de_frase:
            continue   # palavra sozinha no comeco da frase: quase sempre so a maiuscula da frase
        if len(nome) >= 3 and nome not in nomes:
            nomes.append(nome)
    saida = ""
    for n in nomes:
        if len(saida) + len(n) + 2 > limite:
            break
        saida += (", " if saida else "") + n
    return saida


def _juntar_segmentos(segs):
    lista = []
    for sg in segs:   # o Whisper trabalha enquanto isto itera: da para interromper no meio
        checar()
        lista.append(sg)
    return lista


WHISPER = {"modelo": None, "chave": None, "usado": 0.0, "vigia": None}
WHISPER_TRAVA = threading.Lock()
MINUTOS_WHISPER = 10   # sem video nenhum por esse tempo: tira o modelo da placa de video


def _vigiar_whisper():
    """Libera a memoria da placa de video quando o Whisper fica parado."""
    import gc
    import time
    while True:
        time.sleep(30)
        with WHISPER_TRAVA:
            if WHISPER["modelo"] is not None and time.time() - WHISPER["usado"] > MINUTOS_WHISPER * 60:
                WHISPER["modelo"] = WHISPER["chave"] = None
                gc.collect()


def _modelo_whisper(nome, device):
    """O modelo carregado (fica na memoria entre um video e outro). Chamar com WHISPER_TRAVA."""
    import time
    from faster_whisper import WhisperModel
    chave = (nome, device)
    if WHISPER["chave"] != chave:
        WHISPER["modelo"] = None
        WHISPER["modelo"] = WhisperModel(nome, device=device, compute_type="float16" if device == "cuda" else "int8")
        WHISPER["chave"] = chave
    WHISPER["usado"] = time.time()
    if WHISPER["vigia"] is None:
        WHISPER["vigia"] = threading.Thread(target=_vigiar_whisper, daemon=True)
        WHISPER["vigia"].start()
    return WHISPER["modelo"]


def _rodar_whisper(video, gpu, dica):
    """GPU: modo em lotes (bem mais rapido em video longo); se faltar memoria, o modo normal."""
    import time
    nome = "large-v3-turbo" if gpu else "small"
    with WHISPER_TRAVA:
        modelo = _modelo_whisper(nome, "cuda" if gpu else "cpu")
        try:
            if gpu:
                from faster_whisper import BatchedInferencePipeline
                try:
                    segs, info = BatchedInferencePipeline(model=modelo).transcribe(
                        str(video), batch_size=8, word_timestamps=True, **dica)
                    return _juntar_segmentos(segs), info.language
                except Cancelado:
                    raise
                except Exception as e:  # noqa: BLE001
                    if "memory" not in str(e).lower():
                        raise
            segs, info = modelo.transcribe(str(video), vad_filter=True, word_timestamps=True, **dica)
            return _juntar_segmentos(segs), info.language
        finally:
            WHISPER["usado"] = time.time()


def transcrever(video, log, contexto=""):
    preparar_cuda()
    nomes = glossario(contexto)
    dica = {"initial_prompt": "Names: " + nomes + "."} if nomes else {}
    if nomes:
        log(f"  nomes do titulo/descricao para ajudar o Whisper: {nomes[:120]}")
    try:
        import ctranslate2
        gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        gpu = False
    carregado = WHISPER["chave"] == ("large-v3-turbo" if gpu else "small", "cuda" if gpu else "cpu")
    log(f"Transcrevendo com Whisper ({'large-v3-turbo' if gpu else 'small'}, {'GPU' if gpu else 'CPU'})"
        + ("..." if carregado else "... na primeira vez baixa o modelo."))
    try:
        return _rodar_whisper(video, gpu, dica)
    except Cancelado:
        raise
    except Exception as e:  # noqa: BLE001
        if not gpu:
            raise
        log(f"GPU falhou: {str(e)[:300]}")
        log("Usando a CPU (mais lento).")
        return _rodar_whisper(video, False, dica)


def em_trechos(segs):
    """Quebra a fala em trechos curtos de legenda: [(inicio, fim, texto)]."""
    trechos = []
    for seg in segs:
        palavras = seg.words or []
        if not palavras:
            if seg.text.strip():
                trechos.append((seg.start, seg.end, seg.text.strip()))
            continue
        atual, ini, fim = [], None, None

        def fechar():
            nonlocal atual, ini, fim
            if atual:
                trechos.append((ini, fim, "".join(atual).strip()))
            atual, ini, fim = [], None, None

        for w in palavras:
            if atual:
                texto_atual = "".join(atual)
                pausa = w.start - fim > 0.8
                longo = len(texto_atual) + len(w.word) > MAX_CHARS_FONTE
                demorado = w.end - ini > 6.0
                pontuou = texto_atual.rstrip()[-1:] in ".?!" and len(texto_atual) > 25
                if pausa or longo or demorado or pontuou:
                    fechar()
            if ini is None:
                ini = w.start
            atual.append(w.word)
            fim = w.end
        fechar()
    # junta sobras curtas (ex.: uma palavra sozinha) ao trecho anterior
    juntos = []
    for a, b, t in trechos:
        if juntos and len(t) < 15 and a - juntos[-1][1] < 0.8 and len(juntos[-1][2]) + len(t) + 1 <= 2 * MAX_CHARS_LINHA:
            pa, _, pt = juntos[-1]
            juntos[-1] = (pa, b, pt + " " + t)
        else:
            juntos.append((a, b, t))
    trechos = juntos
    # evita sobreposicao e duracao minima
    for i, (a, b, t) in enumerate(trechos):
        if i + 1 < len(trechos):
            b = min(b, trechos[i + 1][0])
        trechos[i] = (a, max(b, a + 0.8), t)
    return trechos


# ----------------------------------------------------------------- traducao
PROMPT = (
    "Traduza para portugues do Brasil as legendas abaixo (idioma de origem: {idioma}). "
    "Tom natural e falado, sem traducao literal de girias. Mantenha nomes proprios, apelidos e termos de MMA "
    "que o publico brasileiro ja usa em ingles (takedown, clinch, ground and pound); traduza o resto "
    "(knockout -> nocaute, submission -> finalizacao). Responda SOMENTE com um array JSON de strings, "
    "com exatamente {n} itens, na mesma ordem.\n\n{json}"
)


PROMPT_EN = (
    "Translate the subtitles below into natural, spoken English (source language: {idioma}). "
    "Do not translate slang literally. Keep proper names and nicknames, and keep MMA terms in their usual English form "
    "(takedown, clinch, ground and pound, knockout, submission). Reply ONLY with a JSON array of strings, "
    "with exactly {n} items, in the same order.\n\n{json}"
)
NOMES_IDIOMA = {"pt": "portugues", "en": "ingles"}


def traduzir_claude(textos, idioma, destino="pt"):
    import anthropic
    cli = anthropic.Anthropic()
    out = []
    for i in range(0, len(textos), LOTE_TRADUCAO):
        lote = textos[i:i + LOTE_TRADUCAO]
        for _ in range(3):
            r = cli.messages.create(
                model=MODELO_CLAUDE, max_tokens=4096,
                messages=[{"role": "user", "content": (PROMPT_EN if destino == "en" else PROMPT).format(
                    idioma=idioma, n=len(lote), json=json.dumps(lote, ensure_ascii=False))}])
            bruto = r.content[0].text
            try:
                arr = json.loads(bruto[bruto.index("["): bruto.rindex("]") + 1])
                if len(arr) == len(lote):
                    out.extend(str(x) for x in arr)
                    break
            except ValueError:
                pass
        else:
            raise RuntimeError("A traducao voltou fora do formato 3 vezes seguidas.")
    return out


def _google_com_retry(tr, texto):
    import time
    for tentativa in range(6):
        try:
            return tr.translate(texto) or texto
        except Exception:  # noqa: BLE001
            if tentativa == 5:
                raise
            time.sleep(2 * (tentativa + 1))


def traduzir_google(textos, idioma, destino="pt"):
    """Junta varias frases por pedido (limite do Google: ~5 pedidos/s) e separa de volta por linha."""
    import time
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target=destino)
    out, i = [], 0
    while i < len(textos):
        lote, tam = [], 0
        while i + len(lote) < len(textos) and len(lote) < 15 and tam < 3500:
            t = textos[i + len(lote)].replace("\n", " ")
            lote.append(t)
            tam += len(t) + 1
        resp = _google_com_retry(tr, "\n".join(lote)).split("\n")
        if len(resp) != len(lote):          # separacao falhou: traduz uma a uma, devagar
            resp = []
            for t in lote:
                resp.append(_google_com_retry(tr, t))
                time.sleep(0.4)
        out.extend(r.strip() for r in resp)
        i += len(lote)
        time.sleep(0.5)
    return out


# ------------------------------------------------------------------- Ollama
OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
# modelos que costumam traduzir bem para portugues, do mais ao menos preferido
PREFERIDOS = ("qwen3", "qwen2.5", "gemma3", "gemma2", "llama3.3", "llama3.1", "mistral-nemo", "mistral", "llama3", "phi4")
LOTE_OLLAMA = 15


def _ollama(caminho, corpo=None, timeout=600):
    import urllib.request
    req = urllib.request.Request(
        OLLAMA_URL + caminho,
        data=None if corpo is None else json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def escolher_modelo_ollama():
    """Devolve o nome do modelo a usar, ou None se o Ollama nao estiver acessivel / sem modelos."""
    try:
        nomes = [m["name"] for m in _ollama("/api/tags", timeout=5).get("models", [])]
    except Exception:  # noqa: BLE001
        return None
    nomes = [n for n in nomes if "embed" not in n.lower()]
    if not nomes:
        return None
    pedido = os.environ.get("OLLAMA_MODEL")
    if pedido:
        return pedido if pedido in nomes else next((n for n in nomes if n.startswith(pedido)), nomes[0])
    for pref in PREFERIDOS:
        achados = [n for n in nomes if n.lower().startswith(pref)]
        if achados:
            return achados[0]
    return nomes[0]


PROMPT_OLLAMA = (
    "Voce e um tradutor de legendas. Traduza para portugues do Brasil cada item da lista (idioma de origem: {idioma}). "
    "Tom natural e falado, sem traducao literal de girias. Mantenha nomes proprios, apelidos e termos de MMA que o "
    "publico brasileiro usa em ingles (takedown, clinch, ground and pound); traduza o resto "
    "(knockout -> nocaute, submission -> finalizacao). Nao junte nem separe itens, nao explique nada. "
    'Responda SOMENTE com JSON no formato {{"t": ["...", "..."]}} com exatamente {n} strings, na mesma ordem.\n\n{json}'
)


PROMPT_OLLAMA_EN = (
    "You are a subtitle translator. Translate each item of the list into natural, spoken English (source language: "
    "{idioma}). Do not translate slang literally. Keep proper names and nicknames, and keep MMA terms in their usual "
    "English form (takedown, clinch, ground and pound, knockout, submission). Do not merge or split items, do not "
    'explain anything. Reply ONLY with JSON in the format {{"t": ["...", "..."]}} with exactly {n} strings, in the same '
    "order.\n\n{json}"
)


def _chat_ollama(modelo, conteudo):
    r = _ollama("/api/chat", {
        "model": modelo, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0.2},
        "messages": [{"role": "user", "content": conteudo}]})
    return r["message"]["content"]


def traduzir_ollama(textos, idioma, modelo, log, destino="pt"):
    prompt = PROMPT_OLLAMA_EN if destino == "en" else PROMPT_OLLAMA
    out = []
    for i in range(0, len(textos), LOTE_OLLAMA):
        lote = textos[i:i + LOTE_OLLAMA]
        resp = None
        for _ in range(3):
            try:
                bruto = _chat_ollama(modelo, prompt.format(
                    idioma=idioma, n=len(lote), json=json.dumps(lote, ensure_ascii=False)))
                arr = json.loads(bruto)["t"]
                if isinstance(arr, list) and len(arr) == len(lote):
                    resp = [str(x).strip() for x in arr]
                    break
            except (ValueError, KeyError, TypeError):
                pass
        if resp is None:   # lote nao veio no formato: traduz uma frase por vez
            resp = []
            for t in lote:
                um = _chat_ollama(modelo, prompt.format(
                    idioma=idioma, n=1, json=json.dumps([t], ensure_ascii=False)))
                try:
                    resp.append(str(json.loads(um)["t"][0]).strip())
                except (ValueError, KeyError, IndexError, TypeError):
                    resp.append(t)
        out.extend(resp)
        log(f"  traduzido {min(i + LOTE_OLLAMA, len(textos))}/{len(textos)}")
    return out


# --------------------------------------------------------------------- Gemini
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com").rstrip("/")
GEMINI_PADRAO = "gemini-3.1-flash-lite"     # Flash-Lite mais barato com plano gratuito
GEMINI_ALTERNATIVOS = ("gemini-3.5-flash-lite", "gemini-2.5-flash-lite")
LOTE_GEMINI = 250        # legendas por chamada: um video de ~15 min cabe em 1 chamada

INSTRUCAO_GEMINI_EN = (
    "Translate subtitles into natural, spoken English; do not translate slang literally. "
    "Keep proper names and nicknames, and keep MMA terms in their usual English form (takedown, clinch, ground and pound, "
    "knockout, submission). Input: one subtitle per line in the format N|text. "
    'Output: JSON {"t":[{"n":N,"t":"translation"},...]} with exactly one item per input line, each with the number of '
    "its line. Never merge or split lines: a sentence that continues in the next line stays split."
)
INSTRUCAO_GEMINI = (
    "Traduza legendas para portugues do Brasil, tom falado e natural, sem traducao literal de girias. "
    "Mantenha nomes proprios e os termos de MMA que o Brasil usa em ingles (takedown, clinch, ground and pound); "
    "knockout=nocaute, submission=finalizacao. Entrada: uma legenda por linha no formato N|texto. "
    'Saida: JSON {"t":[{"n":N,"t":"traducao"},...]} com exatamente um item por linha de entrada, cada um com o numero '
    "da sua linha. Nunca junte nem divida linhas: frase que continua na linha seguinte continua dividida."
)


# idiomas da aba Transcrever: codigo -> (nome na lista, nome em ingles para o tradutor)
LINGUAS = {"pt": ("Português (Brasil)", "Brazilian Portuguese"), "en": ("English", "English"),
           "es": ("Español", "Spanish"), "fr": ("Français", "French"), "it": ("Italiano", "Italian"),
           "de": ("Deutsch", "German"), "ja": ("日本語", "Japanese"), "ko": ("한국어", "Korean"),
           "ru": ("Русский", "Russian"), "zh": ("中文", "Simplified Chinese")}
INSTRUCAO_GEMINI_GENERICA = (
    "Translate subtitles into natural, spoken {lingua}; do not translate slang literally. "
    "Keep proper names and nicknames. Input: one subtitle per line in the format N|text. "
    'Output: JSON {{"t":[{{"n":N,"t":"translation"}},...]}} with exactly one item per input line, each with the number '
    "of its line. Never merge or split lines: a sentence that continues in the next line stays split."
)


class ErroGemini(RuntimeError):
    def __init__(self, msg, codigo=None, espera=None, diario=False):
        super().__init__(msg)
        self.codigo, self.espera, self.diario = codigo, espera, diario


def _gemini(caminho, corpo, timeout=180):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        GEMINI_BASE + caminho, data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        bruto = e.read().decode("utf-8", "replace")
        msg, espera = e.reason, None
        try:
            err = json.loads(bruto)["error"]
            msg = err.get("message", msg)
            for d in err.get("details", []):
                if "retryDelay" in d:
                    espera = float(str(d["retryDelay"]).rstrip("s") or 0)
        except Exception:  # noqa: BLE001
            pass
        diario = "PerDay" in bruto or "per day" in bruto.lower()
        raise ErroGemini(f"HTTP {e.code}: {str(msg)[:200]}", e.code, espera, diario) from None


ESTADO_GEMINI = {"thinking_off": True}   # o que vale para o programa todo (cada video tem a sua "sessao")

PEDIDO_TITULO = (
    " Alem disso, no campo \"titulo\", escreva um titulo curto em portugues do Brasil (no maximo 8 palavras, "
    "sem hashtags, sem emojis, sem aspas) que diga do que o video trata, usando a descricao do post abaixo "
    "(se ela for util) e as legendas.\nDescricao do post: ")


CONTEXTO_PT = (" Contexto do video (titulo e descricao do post): use para acertar a grafia de nomes de pessoas, "
               "lutadores, eventos e termos. A fala foi transcrita automaticamente e pode ter nomes escritos errado: "
               "quando um nome da fala soar parecido com um nome do contexto, use a grafia do contexto.\nContexto: ")
CONTEXTO_EN = (" Video context (title and post description): use it to get the spelling of people, fighters, events and "
               "terms right. The speech was transcribed automatically and names may be misspelled: when a name in the "
               "speech sounds like a name in the context, use the context's spelling.\nContext: ")


def _chamar_gemini(modelo, linhas, sessao, descricao=None, destino="pt", contexto=""):
    """Uma chamada: recebe [(numero, texto), ...] e devolve {numero: traducao} (cada traducao volta com o numero
    da linha, entao uma resposta com linhas a mais ou a menos nao estraga as outras).
    Com descricao (mesmo vazia) tambem pede um titulo, guardado em sessao['titulo']."""
    import time
    props = {"t": {"type": "ARRAY", "items": {"type": "OBJECT", "required": ["n", "t"], "properties": {
        "n": {"type": "INTEGER"}, "t": {"type": "STRING"}}}}}
    if destino == "pt":
        instrucao = INSTRUCAO_GEMINI
    elif destino == "en":
        instrucao = INSTRUCAO_GEMINI_EN
    else:
        instrucao = INSTRUCAO_GEMINI_GENERICA.format(lingua=LINGUAS.get(destino, (destino, destino))[1])
    if (contexto or "").strip():
        instrucao += (CONTEXTO_PT if destino == "pt" else CONTEXTO_EN) + contexto.strip()[:1500]
    if descricao is not None:
        props["titulo"] = {"type": "STRING"}
        instrucao += PEDIDO_TITULO + (descricao.strip()[:1500] or "(sem descricao)")
    config = {"temperature": 0.2, "maxOutputTokens": 16384, "responseMimeType": "application/json",
              "responseSchema": {"type": "OBJECT", "required": ["t"], "properties": props}}
    texto = "\n".join(f"{n}|{t.replace(chr(10), ' ')}" for n, t in linhas)
    for tentativa in range(3):
        corpo = {"systemInstruction": {"parts": [{"text": instrucao}]},
                 "contents": [{"role": "user", "parts": [{"text": texto}]}],
                 "generationConfig": dict(config)}
        if ESTADO_GEMINI["thinking_off"]:
            corpo["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}   # raciocinio gasta token a toa
        try:
            r = _gemini(f"/v1beta/models/{modelo}:generateContent", corpo)
        except ErroGemini as e:
            if e.codigo == 400 and ESTADO_GEMINI["thinking_off"] and "think" in str(e).lower():
                ESTADO_GEMINI["thinking_off"] = False      # modelo nao aceita a opcao: manda sem ela
                continue
            if e.codigo == 429 and e.diario:
                raise ErroGemini("cota DIARIA do plano gratuito acabou; tente amanha", 429) from None
            if e.codigo in (429, 500, 503) and tentativa < 2:
                for _ in range(int(min(e.espera or 20, 65))):   # espera o que o proprio Google pediu
                    checar()
                    time.sleep(1)
                continue
            raise
        checar()
        sessao["chamadas"] += 1
        uso = r.get("usageMetadata", {})
        sessao["tokens_in"] += uso.get("promptTokenCount", 0)
        sessao["tokens_out"] += uso.get("candidatesTokenCount", 0) + uso.get("thoughtsTokenCount", 0)
        try:
            dados = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError, TypeError):
            return {}
        if descricao is not None and str(dados.get("titulo") or "").strip():
            sessao["titulo"] = str(dados["titulo"]).strip()
        pedidos = {n for n, _ in linhas}
        out = {}
        for item in dados.get("t") or []:
            try:
                n, t = int(item["n"]), str(item["t"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
            if n in pedidos and t and n not in out:
                out[n] = t
        return out
    raise ErroGemini("o Gemini nao respondeu depois de 3 tentativas")


def traduzir_gemini(textos, idioma, log, descricao=None, destino="pt", contexto="", sessao=None):
    """Traduz em lotes; o que faltar numa resposta e pedido de novo (so as linhas que faltaram)."""
    modelos = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else [GEMINI_PADRAO, *GEMINI_ALTERNATIVOS]
    sessao = sessao if sessao is not None else {}
    for k in ("chamadas", "tokens_in", "tokens_out"):
        sessao[k] = 0
    ultimo = None
    for modelo in modelos:
        try:
            log(f"Traduzindo com o Gemini ({modelo})...")
            feitos = {}
            for i in range(0, len(textos), LOTE_GEMINI):
                faltam = [(n + 1, textos[n]) for n in range(i, min(i + LOTE_GEMINI, len(textos)))]
                for rodada in range(3):
                    if not faltam:
                        break
                    if rodada:
                        log(f"  pedindo de novo {len(faltam)} linha(s) que vieram faltando...")
                    feitos.update(_chamar_gemini(modelo, faltam, sessao,
                                                 descricao if (i == 0 and rodada == 0) else None, destino, contexto))
                    sem = {n for n, _ in faltam if n not in feitos}
                    # linha que sumiu quase sempre foi juntada com a de antes: pede as duas de novo
                    pedir = sem | {n - 1 for n in sem if n - 1 > i}
                    faltam = [(n, textos[n - 1]) for n in sorted(pedir)]
                faltam = [(n, t) for n, t in faltam if n not in feitos]
                if faltam:
                    log(f"  {len(faltam)} linha(s) ficaram sem traducao (mantive o original)")
                log(f"  traduzido {min(i + LOTE_GEMINI, len(textos))}/{len(textos)}")
            log(f"  Gemini: {sessao['chamadas']} chamada(s), {sessao['tokens_in']} tokens de entrada, "
                f"{sessao['tokens_out']} de saida.")
            return [feitos.get(n + 1, t) for n, t in enumerate(textos)]
        except ErroGemini as e:
            ultimo = e
            if e.codigo == 404 or (e.codigo in (500, 503) and not e.diario):   # sem o modelo ou ocupado: proximo
                continue
            raise
    raise ultimo


def traduzir(textos, idioma, log, descricao=None, destino="pt", contexto="", sessao=None):
    """sessao: dict deste video; o Gemini deixa ali o titulo sugerido (sessao['titulo'])."""
    if os.environ.get("GEMINI_API_KEY"):
        try:
            return traduzir_gemini(textos, idioma, log, descricao, destino, contexto, sessao)
        except Exception as e:  # noqa: BLE001
            log(f"Gemini falhou: {str(e)[:200]}")
    else:
        log(f"Sem GEMINI_API_KEY ({'arquivo ' + ENV_LIDO + ' lido' if ENV_LIDO else 'arquivo .env NAO encontrado na pasta'}).")
    modelo = escolher_modelo_ollama()
    if modelo:
        try:
            log(f"Traduzindo com o Ollama (modelo {modelo})...")
            return traduzir_ollama(textos, idioma, modelo, log, destino)
        except Exception as e:  # noqa: BLE001
            log(f"Ollama falhou ({str(e)[:100]}).")
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            log("Traduzindo com Claude...")
            return traduzir_claude(textos, idioma, destino)
        except Exception as e:  # noqa: BLE001
            log(f"Claude falhou ({str(e)[:80]}).")
    log("Usando o tradutor do Google (qualidade menor).")
    return traduzir_google(textos, idioma, destino)


# -------------------------------------------------------------- srt + video
# ---------------------------------------------------------- montagem das legendas
# ------------------------------------------------------------ estilo da legenda
ESTILO_PADRAO = {
    "fonte": "Arial", "tamanho": 100, "negrito": True, "cor": "#FFFFFF",
    "contorno": True, "cor_contorno": "#000000", "espessura": 9,          # % do tamanho da fonte
    "sombra": True, "cor_sombra": "#000000", "dist_sombra": 5, "opac_sombra": 60,
    "tarja": False, "cor_tarja": "#000000", "opac_tarja": 70,
    "altura": 100,                                                       # % da distancia padrao da borda de baixo
}
PASTA_FONTES = PASTA_APP / "fontes"


def estilo_completo(est):
    e = dict(ESTILO_PADRAO)
    e.update({k: v for k, v in (est or {}).items() if k in ESTILO_PADRAO})
    return e


def estilos_salvos():
    """{nome: estilo} e o nome do estilo em uso (os proximos videos usam ele)."""
    cfg = ler_config()
    est = {n: estilo_completo(e) for n, e in (cfg.get("estilos") or {}).items()} or {"Padrão": dict(ESTILO_PADRAO)}
    uso = cfg.get("estilo_em_uso")
    return est, (uso if uso in est else next(iter(est)))


def familias_da_pasta_fontes():
    """Nomes das fontes (.ttf/.otf) que estao na pasta 'fontes' do programa (sem precisar instalar no Windows)."""
    import struct
    nomes = set()
    for arq in list(PASTA_FONTES.glob("*.ttf")) + list(PASTA_FONTES.glob("*.otf")) if PASTA_FONTES.is_dir() else []:
        try:
            d = arq.read_bytes()
            n_tab = struct.unpack(">H", d[4:6])[0]
            for i in range(n_tab):
                tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
                if tag != b"name":
                    continue
                _, cont, base = struct.unpack(">HHH", d[off:off + 6])
                achado = None
                for j in range(cont):
                    plat, enc, lang, nid, tam, o = struct.unpack(">HHHHHH", d[off + 6 + 12 * j: off + 18 + 12 * j])
                    if nid not in (1, 16):
                        continue
                    bruto = d[off + base + o: off + base + o + tam]
                    txt = bruto.decode("utf-16-be", "ignore") if plat in (0, 3) else bruto.decode("latin-1")
                    if txt and (achado is None or nid == 16):
                        achado = txt
                if achado:
                    nomes.add(achado.strip())
        except Exception:  # noqa: BLE001
            pass
    return sorted(nomes)


def filtro_ass(nome_ass, pasta_trabalho):
    """Filtro do ffmpeg para gravar a legenda. Se houver fontes na pasta 'fontes', copia para a pasta de trabalho
    (caminho simples, sem os problemas de C:\\ no filtro) e manda o ffmpeg procurar ali."""
    import shutil
    if PASTA_FONTES.is_dir() and any(PASTA_FONTES.iterdir()):
        destino = Path(pasta_trabalho) / "fontes"
        destino.mkdir(exist_ok=True)
        for f in PASTA_FONTES.iterdir():
            if f.suffix.lower() in (".ttf", ".otf", ".ttc") and not (destino / f.name).exists():
                shutil.copy2(f, destino / f.name)
        return f"ass={nome_ass}:fontsdir=fontes"
    return f"ass={nome_ass}"


def medidas(larg, alt, escala=100):
    """Tamanho da fonte (px) e caracteres por linha para o video, sem distorcer nada.
    escala: tamanho do estilo em % (100 = tamanho original do programa)."""
    vertical = alt > larg
    fs = (larg * 0.058 if vertical else alt * 0.05) * escala / 100
    fs = max(8, round(fs))
    maxc = int(larg * 0.88 / (0.53 * fs))
    return fs, max(8, min(maxc, 42))


def _duas_linhas(texto, maxc):
    """Quebra em 1 ou 2 linhas equilibradas; None se nao couber em 2."""
    texto = " ".join(texto.split())
    if len(texto) <= maxc:
        return [texto]
    palavras = texto.split(" ")
    melhor = None
    for i in range(1, len(palavras)):
        l1, l2 = " ".join(palavras[:i]), " ".join(palavras[i:])
        if len(l1) > maxc or len(l2) > maxc:
            continue
        custo = abs(len(l1) - len(l2)) - (6 if l1[-1] in ",.;:?!" else 0)
        if melhor is None or custo < melhor[0]:
            melhor = (custo, [l1, l2])
    return melhor[1] if melhor else None


def _partir(texto, maxc):
    """Divide um texto longo em pedacos que cabem em 2 linhas, cortando de preferencia na pontuacao."""
    if _duas_linhas(texto, maxc):
        return [texto]
    palavras = texto.split()
    n = max(2, -(-len(texto) // int(2 * maxc * 0.85)))
    alvo = len(texto) / n
    melhor_i, melhor_custo, acc = 1, None, 0
    for i in range(1, len(palavras)):
        acc += len(palavras[i - 1]) + 1
        custo = abs(acc - alvo) - (12 if palavras[i - 1][-1] in ",.;:?!" else 0)
        if melhor_custo is None or custo < melhor_custo:
            melhor_i, melhor_custo = i, custo
    return _partir(" ".join(palavras[:melhor_i]), maxc) + _partir(" ".join(palavras[melhor_i:]), maxc)


def montar_legendas(trechos, textos, maxc):
    """[(inicio, fim, [linha1, linha2?])] com no maximo 2 linhas e sem palavra sozinha numa legenda."""
    base = [[a, b, " ".join(t.split())] for (a, b, _), t in zip(trechos, textos) if t.strip()]
    juntos = []
    for c in base:   # junta sobras curtas (ex.: "reacoes.") com a legenda anterior
        if juntos and len(c[2]) < 16 and c[0] - juntos[-1][1] < 0.6:
            juntos[-1][1], juntos[-1][2] = c[1], juntos[-1][2] + " " + c[2]
        else:
            juntos.append(c)
    saida = []
    for ini, fim, texto in juntos:
        partes = _partir(texto, maxc)
        total = sum(len(p) for p in partes)
        t = ini
        for p in partes:
            dur = (fim - ini) * len(p) / total
            saida.append((t, t + dur, _duas_linhas(p, maxc) or [p]))
            t += dur
    return saida


def escrever_srt(legendas, caminho):
    linhas = []
    for n, (a, b, ls) in enumerate(legendas, 1):
        linhas.append(f"{n}\n{tempo_srt(a)} --> {tempo_srt(b)}\n" + "\n".join(ls) + "\n")
    Path(caminho).write_text("\n".join(linhas), encoding="utf-8")


# ------------------------------------------------- legendas que ja existem no video
PROMPT_TELA = (
    "Cada imagem numerada e um quadro de um video. Para cada uma, devolva as caixas de TEXTO SOBREPOSTO "
    "que ocupa a tela: legendas ja gravadas no video, titulos, letreiros, nomes. Ignore logotipos e marcas "
    "d'agua pequenas no canto. Caixas no formato [ymin, xmin, ymax, xmax] de 0 a 1000. Sem texto: lista vazia."
)


def _pedir_caixas(modelo, esquema, lote):
    """Manda um lote de quadros ao Gemini. Devolve {indice_no_lote: [(y0, x0, y1, x1), ...]} de 0 a 1."""
    import base64
    import time
    partes = [{"text": PROMPT_TELA}]
    for n, (_, arq) in enumerate(lote):
        partes.append({"text": f"Imagem {n}"})
        partes.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(arq.read_bytes()).decode()}})
    corpo = {"contents": [{"role": "user", "parts": partes}],
             "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                  "responseSchema": esquema, "thinkingConfig": {"thinkingBudget": 0},
                                  "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
    caminho = f"/v1beta/models/{modelo}:generateContent"
    for tentativa in range(3):
        try:
            r = _gemini(caminho, corpo)
            break
        except ErroGemini as e:
            if e.codigo == 400 and "thinkingConfig" in corpo["generationConfig"]:
                for chave in ("thinkingConfig", "mediaResolution"):   # opcoes que o modelo pode nao aceitar
                    corpo["generationConfig"].pop(chave, None)
            elif e.codigo == 429 and not e.diario and tentativa < 2:
                time.sleep(min(e.espera or 10, 30))
            else:
                raise
    res = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])["q"]
    out = {}
    for item in res:
        n = item.get("i")
        if isinstance(n, int) and 0 <= n < len(lote):
            for bx in item.get("b") or []:
                if len(bx) == 4:
                    y0, x0, y1, x1 = (max(0, min(1000, v)) / 1000 for v in bx)
                    if y1 > y0 and x1 > x0:
                        out.setdefault(n, []).append((y0, x0, y1, x1))
    return out


def _amostrar_tela(video, duracao, espaco, log, falhas=None):
    """Tira quadros pequenos a cada `espaco` segundos (varios ffmpeg ao mesmo tempo) e pergunta ao Gemini,
    em lotes paralelos, onde ha texto em cada um. Devolve [(segundo, [caixas])]."""
    import shutil
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    tempos = [espaco * (k + 0.5) for k in range(int(duracao / espaco) + 1) if espaco * (k + 0.5) < duracao] \
        or [duracao / 2]
    tmp = Path(tempfile.mkdtemp(prefix="fc_quadros_"))
    try:
        def extrair(k):
            arq = tmp / f"{k:04d}.jpg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{tempos[k]:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", str(arq)], check=False)
            return (tempos[k], arq) if arq.exists() else None
        esquema = {"type": "OBJECT", "required": ["q"], "properties": {"q": {"type": "ARRAY", "items": {
            "type": "OBJECT", "required": ["i", "b"], "properties": {
                "i": {"type": "INTEGER"},
                "b": {"type": "ARRAY", "items": {"type": "ARRAY", "items": {"type": "INTEGER"}}}}}}}}
        modelo = os.environ.get("GEMINI_MODEL") or GEMINI_PADRAO

        def analisar(lote):
            try:
                return _pedir_caixas(modelo, esquema, lote)
            except Exception as e:  # noqa: BLE001
                log(f"  nao consegui analisar parte da tela ({str(e)[:100]})")
                if falhas is not None:
                    falhas.append(1)   # resultado incompleto: nao vale guardar
                return {}

        def rodar(indices):
            """Extrai e analisa os quadros indicados (lotes de 40 em paralelo). Devolve [(segundo, [caixas])]."""
            with ThreadPoolExecutor(6) as ex:
                quadros = [q for q in ex.map(extrair, indices) if q]
            lotes = [quadros[k:k + 40] for k in range(0, len(quadros), 40)]
            with ThreadPoolExecutor(3) as ex:
                resultados = list(ex.map(analisar, lotes))
            return [(t, res.get(n, [])) for lote, res in zip(lotes, resultados) for n, (t, _) in enumerate(lote)]

        # 1) sonda: poucos quadros do comeco do video. Se nao tem texto nenhum, nao gasta mais tempo.
        inicio = [k for k, t in enumerate(tempos) if t <= 30] or list(range(len(tempos)))
        passo = max(1, len(inicio) / 6)
        sonda = sorted({inicio[int(i * passo)] for i in range(min(6, len(inicio)))})
        amostras = rodar(sonda)
        if not any(cx for _, cx in amostras):
            log("  nenhuma legenda ja gravada no comeco do video; pulei o resto da analise.")
            return sorted(amostras)
        # 2) achou texto: olha o resto do video
        amostras += rodar([k for k in range(len(tempos)) if k not in sonda])
        log(f"  analise da tela: {len(amostras)} quadros")
        return sorted(amostras)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def iniciar_analise_da_tela(video, duracao, log):
    """Comeca a olhar a tela em segundo plano, junto com a transcricao (o Gemini trabalha na internet e o
    Whisper na placa de video, entao um nao atrasa o outro)."""
    estado = {"amostras": [], "espaco": max(2.0, duracao / 90), "thread": None, "falhas": []}
    if not os.environ.get("GEMINI_API_KEY"):
        return estado

    def rodar():
        try:
            estado["amostras"] = _amostrar_tela(video, duracao, estado["espaco"], log, estado["falhas"])
        except Exception as e:  # noqa: BLE001
            estado["falhas"].append(1)
            log(f"  nao consegui analisar a tela ({str(e)[:100]}); a legenda fica na posicao padrao.")
    estado["thread"] = threading.Thread(target=rodar, daemon=True)
    estado["thread"].start()
    return estado


PASTA_CACHE = PASTA_APP / "cache"


def _arq_analise(caminho, duracao):
    """Arquivo onde fica guardada a analise da tela de um video (chave: caminho + duracao em segundos; assim ela
    continua valendo depois que o original e convertido para o Premiere)."""
    import hashlib
    chave = f"{Path(caminho).resolve()}|{int(duracao)}".lower()
    return PASTA_CACHE / f"tela_{hashlib.sha1(chave.encode('utf-8')).hexdigest()[:16]}.json"


DIAS_CACHE = 7


def limpar_cache():
    """Apaga da pasta cache o que foi guardado ha mais de DIAS_CACHE dias."""
    import time
    limite = time.time() - DIAS_CACHE * 86400
    try:
        for f in [*PASTA_CACHE.glob("tela_*.json"), *PASTA_CACHE.glob("trechos_*.json")]:
            try:
                if f.stat().st_mtime < limite:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def limpar_temporarios(horas=1):
    """Apaga as pastas .fc_tmp_ que sobraram de videos interrompidos (so as do proprio programa, paradas ha mais de
    `horas`), na pasta de saida e nas pastas dos videos da lista."""
    import shutil
    import time
    cfg = ler_config()
    pastas = {str(SAIDA_BASE)} | {str(v.get("base")) for v in cfg.get("videos") or [] if v.get("base")}
    limite = time.time() - horas * 3600
    for p in pastas:
        try:
            for d in Path(p).glob(".fc_tmp_*"):
                if d.is_dir() and all(f.stat().st_mtime < limite for f in [d, *d.rglob("*")]):
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def salvar_analise(estado, caminho, duracao):
    """Guarda a analise da tela (so se terminou inteira, sem falhas) para nao repetir ao refazer a legenda."""
    th = estado.get("thread") if estado else None
    if th is None or th.is_alive() or estado.get("falhas"):
        return
    try:
        PASTA_CACHE.mkdir(exist_ok=True)
        _arq_analise(caminho, duracao).write_text(
            json.dumps({"espaco": estado["espaco"], "amostras": estado["amostras"]}), encoding="utf-8")
    except OSError:
        pass


def carregar_analise(caminho, duracao):
    """Analise guardada de um video, ou None."""
    try:
        d = json.loads(_arq_analise(caminho, duracao).read_text(encoding="utf-8"))
        return {"amostras": [(t, cx) for t, cx in d["amostras"]], "espaco": float(d["espaco"]), "thread": None,
                "falhas": []}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def caixas_por_legenda(estado, legendas, log):
    """Espera a analise terminar (se ainda nao terminou) e junta, para cada legenda, as caixas de texto
    dos quadros que caem durante ela."""
    th = estado.get("thread")
    if th and th.is_alive():
        log("Procurando legendas que ja existem no video...")
        th.join()
    amostras, esp = estado["amostras"], estado["espaco"]
    caixas = []
    for a, b, _ in legendas:
        dentro = [cx for t, cx in amostras if a - esp / 2 <= t <= b + esp / 2]
        if not dentro and amostras:
            t, cx = min(amostras, key=lambda x: abs(x[0] - (a + b) / 2))
            dentro = [cx]
        caixas.append([bx for cx in dentro for bx in cx])
    return caixas


def escolher_posicoes(legendas, caixas, larg, alt, fs, altura=100):
    """Para cada legenda: (alinhamento ASS, margem vertical em px) sem cobrir o texto que ja esta na tela."""
    vertical = alt > larg
    margem_padrao = round(alt * (0.09 if vertical else 0.05) * altura / 100)
    folga = alt * 0.015
    pos = []
    for (_, _, linhas), cx in zip(legendas, caixas):
        h = len(linhas) * fs * 1.25 + fs * 0.3
        w = max(len(l) for l in linhas) * 0.53 * fs
        x0, x1 = (larg - w) / 2, (larg + w) / 2
        obst = [(y0 * alt, x0b * larg, y1 * alt, x1b * larg) for y0, x0b, y1, x1b in cx
                if (y1 - y0) < 0.6]   # ignora "caixas" do tamanho da tela inteira

        def livre(topo, base):
            if topo < alt * 0.02 or base > alt * 0.98:
                return False
            return all(base + folga <= oy0 or topo - folga >= oy1 or x1 <= ox0 or x0 >= ox1
                       for oy0, ox0, oy1, ox1 in obst)

        # lugar preferido = o do estilo (sempre dentro da tela); se ja houver texto gravado ali,
        # a legenda vai para o lugar livre mais perto dele: o desvio tem prioridade sobre o estilo
        alvo = max(alt * 0.02 + h + 1, min(alt - margem_padrao, alt * 0.98 - 1))
        candidatos = [alvo]   # posicoes da BASE da legenda
        for oy0, _, oy1, _ in obst:
            candidatos += [oy0 - folga, oy1 + folga + h]
        candidatos += [alt * 0.06 + h, alt * 0.97]   # topo e pe da tela, ultimos recursos
        boas = [c for c in candidatos if livre(c - h, c)]
        base = min(boas, key=lambda c: abs(c - alvo)) if boas else alvo
        pos.append((2, round(alt - base)))
    return pos


def _cor_ass(hexa, opacidade=100):
    """'#RRGGBB' -> '&HAABBGGRR' (no ASS, AA=00 e opaco)."""
    hexa = (hexa or "#FFFFFF").lstrip("#")
    if len(hexa) != 6:
        hexa = "FFFFFF"
    aa = round(255 * (1 - max(0, min(100, opacidade)) / 100))
    return f"&H{aa:02X}{hexa[4:6]}{hexa[2:4]}{hexa[0:2]}".upper()


def escrever_ass(legendas, posicoes, caminho, larg, alt, fs, estilo=None):
    """ASS com a resolucao real do video: a fonte nao e esticada nem amassada."""
    e = estilo_completo(estilo)

    def t(x):
        cs = int(round(x * 100))
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"
    if e["tarja"]:   # caixa atras do texto (BorderStyle 3: a "borda" vira a tarja)
        borda, contorno = 3, max(2, round(fs * 0.22))
        cor_borda = _cor_ass(e["cor_tarja"], e["opac_tarja"])
    else:
        borda = 1
        contorno = round(fs * e["espessura"] / 100) if e["contorno"] else 0
        cor_borda = _cor_ass(e["cor_contorno"])
    sombra = round(fs * e["dist_sombra"] / 100) if e["sombra"] else 0
    cor_sombra = _cor_ass(e["cor_sombra"], e["opac_sombra"])
    fonte = str(e["fonte"]).replace(",", " ") or "Arial"
    cab = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {larg}\nPlayResY: {alt}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{fonte},{fs},{_cor_ass(e['cor'])},{_cor_ass(e['cor'])},{cor_borda},{cor_sombra},"
        f"{-1 if e['negrito'] else 0},0,0,0,100,100,0,0,{borda},"
        f"{contorno},{sombra},2,{round(larg * 0.04)},{round(larg * 0.04)},{round(alt * 0.05)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    for (a, b, linhas), (alin, mv) in zip(legendas, posicoes):
        txt = "\\N".join(l.replace("{", "(").replace("}", ")") for l in linhas)
        ev.append(f"Dialogue: 0,{t(a)},{t(b)},Default,,0,0,{mv},,{{\\an{alin}}}{txt}")
    Path(caminho).write_text(cab + "\n".join(ev) + "\n", encoding="utf-8")


TEXTO_PREVIA = {"pt": "Eu sempre disse que ia voltar para lutar pelo cinturão, e agora finalmente chegou a hora certa.",
                "en": "I always said I would come back to fight for the belt, and now the time has finally come."}


def renderizar_previa(video, momento, larg, alt, estilo, saida, max_w, max_h, idioma="pt"):
    """Grava a legenda de exemplo num quadro, com o MESMO motor da legenda final, e reduz para caber na tela.
    video=None usa um fundo neutro do tamanho larg x alt."""
    import shutil
    import tempfile
    e = estilo_completo(estilo)
    fs, maxc = medidas(larg, alt, e["tamanho"])
    palavras, linhas = TEXTO_PREVIA.get(idioma, TEXTO_PREVIA["pt"]).split(), ["", ""]
    for w in palavras:   # enche duas linhas (o pior caso), corta o resto
        for k in (0, 1):
            if not linhas[k] or len(linhas[k]) + 1 + len(w) <= maxc:
                if k == 0 and linhas[1]:
                    continue
                linhas[k] = (linhas[k] + " " + w).strip()
                break
        else:
            break
    linhas = [l for l in linhas if l]
    tmp = Path(tempfile.mkdtemp(prefix="fc_previa_"))
    try:
        pos = escolher_posicoes([(0, 5, linhas)], [[]], larg, alt, fs, e["altura"])
        escrever_ass([(0, 5, linhas)], pos, tmp / "previa.ass", larg, alt, fs, e)
        esc = min(max_w / larg, max_h / alt, 1)
        w, h = max(2, int(larg * esc) // 2 * 2), max(2, int(alt * esc) // 2 * 2)
        if video:
            entrada = ["-ss", f"{max(0, momento):.2f}", "-i", str(Path(video).resolve())]
        else:
            entrada = ["-f", "lavfi", "-i", f"color=c=0x30343c:s={larg}x{alt}:d=1"]
        filtro = f"setpts=PTS-STARTPTS,{filtro_ass('previa.ass', tmp)},scale={w}:{h}:flags=lanczos"
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + entrada + ["-frames:v", "1", "-vf", filtro,
                           "saida.png"], cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0 or not (tmp / "saida.png").exists():
            raise RuntimeError(r.stderr.strip()[-200:] or "ffmpeg nao gerou a previa")
        shutil.move(str(tmp / "saida.png"), str(saida))
        return saida
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_NVENC = []


def _tem_nvenc():
    """O ffmpeg tem o codificador da NVIDIA? (pergunta uma vez so)"""
    if not _NVENC:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
            _NVENC.append("h264_nvenc" in r.stdout)
        except OSError:
            _NVENC.append(False)
    return _NVENC[0]




def queimar(video, ass, saida, log=print, extra=None):
    """Grava a legenda no video. extra: se vier um caminho, grava ali NA MESMA PASSADA (o video e lido uma vez so)
    uma copia sem legenda pronta para o Premiere (H.264, keyframe a cada ~1 s, sem B-frames)."""
    g = str(_gop(video))   # keyframe a cada ~1 s e sem B-frames: o Premiere le sem "datamosh"
    filtro = filtro_ass(Path(ass).name, Path(ass).resolve().parent)
    pasta = Path(ass).resolve().parent   # o filtro ass= e a saida usam nomes simples nesta pasta
    comum = ["-pix_fmt", "yuv420p", "-g", g, "-bf", "0"]
    entrada = str(Path(video).resolve())

    def montar(enc_leg, enc_ori, audio, hw):
        cmd = ["ffmpeg", "-y", "-loglevel", "error"] + (["-hwaccel", "cuda"] if hw else []) + ["-i", entrada]
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-vf", filtro] + enc_leg + comum + audio + \
            ["-movflags", "+faststart", Path(saida).name]
        if extra:
            cmd += ["-map", "0:v:0", "-map", "0:a?"] + enc_ori + comum + audio + \
                ["-movflags", "+faststart", str(Path(extra).resolve())]
        return cmd
    copia, aac = ["-c:a", "copy"], ["-c:a", "aac", "-b:a", "320k"]   # audio sem perda; aac se nao der
    tentativas = []
    if _tem_nvenc():
        tentativas += [("nv", montar(NV_LEGENDADO, NV_ORIGINAL, a, hw)) for hw in (True, False) for a in (copia, aac)]
    tentativas += [("cpu", montar(X264_LEGENDADO, X264_ORIGINAL, a, False)) for a in (copia, aac)]
    erro, avisou = "", None
    for tipo, cmd in tentativas:
        if tipo != avisou:
            log("Codificando na placa de video (NVENC)..." if tipo == "nv" else
                ("NVENC falhou, codificando no processador (mais lento)..." if avisou else
                 "Codificando no processador..."))
            avisou = tipo
        r = subprocess.run(cmd, cwd=pasta, capture_output=True, text=True)
        checar()
        if r.returncode == 0 and (pasta / Path(saida).name).exists() and (not extra or Path(extra).exists()):
            return
        erro = r.stderr.strip()[-300:]
    raise RuntimeError("ffmpeg falhou: " + erro)


def _fps(video):
    try:
        return _sondar(video)[3]
    except Exception:  # noqa: BLE001
        return 30.0


def _gop(video):
    """Quadros entre keyframes: 1 segundo."""
    return max(1, round(_fps(video)))


def motivo_para_converter(video):
    """Diz por que o video nao esta pronto para edicao no Premiere (ou None se esta bom).
    Bom = H.264, sem B-frames e keyframe pelo menos a cada 2 segundos."""
    def ff(*a):
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *a, str(video)],
                              capture_output=True, text=True).stdout
    codec = ff("-show_entries", "stream=codec_name", "-of", "csv=p=0").strip()
    if codec != "h264":
        return f"codec {codec or 'desconhecido'}"
    tipos = ff("-show_frames", "-show_entries", "frame=pict_type", "-read_intervals", "%+#300", "-of", "csv=p=0")
    if "B" in tipos.split():
        return "tem B-frames"
    tempos = []
    for linha in ff("-show_packets", "-show_entries", "packet=pts_time,flags", "-read_intervals", "%+180",
                    "-of", "csv=p=0").splitlines():
        t, _, f = linha.partition(",")
        try:
            if "K" in f:
                tempos.append(float(t))
        except ValueError:
            pass
    if len(tempos) < 2:
        return "keyframes muito espacados"
    if max(b - a for a, b in zip(tempos, tempos[1:])) > 2.05:
        return "keyframes muito espacados"
    return None


def preparar_original(arquivo, log=print):
    """Se o original nao for bom para o Premiere, recodifica (H.264, keyframe a cada ~1 s, sem B-frames) numa
    copia e troca no fim. Devolve o caminho final (ou None se nao precisou converter)."""
    arquivo = Path(arquivo)
    motivo = motivo_para_converter(arquivo)
    if not motivo:
        log("Original ja esta bom para o Premiere.")
        return None
    log(f"Preparando o original para o Premiere ({motivo})...")
    for velho in arquivo.parent.glob(".fc_tmp_edit_*"):
        velho.unlink(missing_ok=True)
    tmp = arquivo.with_name(f".fc_tmp_edit_{arquivo.name}")
    g = str(_gop(arquivo))
    audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(arquivo)], capture_output=True, text=True).stdout.strip()
    a_cod = ["-c:a", "copy"] if audio in ("aac", "mp3", "") else ["-c:a", "aac", "-b:a", "320k"]
    base = ["-i", str(arquivo), "-map", "0:v:0", "-map", "0:a?", "-g", g, "-bf", "0", "-pix_fmt", "yuv420p"]
    fim = a_cod + ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
    nv, x2 = NV_ORIGINAL, X264_ORIGINAL
    tentativas = []
    if _tem_nvenc():
        tentativas.append(["ffmpeg", "-y", "-loglevel", "error", "-hwaccel", "cuda"] + base + nv + fim)   # decodifica na placa
        tentativas.append(["ffmpeg", "-y", "-loglevel", "error"] + base + nv + fim)
    tentativas.append(["ffmpeg", "-y", "-loglevel", "error"] + base + x2 + fim)
    erro = ""
    for cmd in tentativas:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            checar()
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            break
        erro = r.stderr.strip()[-200:]
        tmp.unlink(missing_ok=True)
    else:
        raise RuntimeError("nao consegui converter o original: " + erro)
    try:
        os.replace(tmp, arquivo)
        destino = arquivo
    except PermissionError:   # o Premiere (ou outro programa) esta com o original aberto
        destino = arquivo.with_name(arquivo.stem + "_edicao.mp4")
        os.replace(tmp, destino)
        log(f"O original estava aberto em outro programa; a versao para edicao ficou em {destino.name}")
    log(f"Original pronto para o Premiere: {destino.name}")
    return destino


def nome_livre(destino):
    """Se o nome ja existe, acrescenta (2), (3)..."""
    destino = Path(destino)
    n, cand = 2, destino
    while cand.exists():
        cand = destino.with_name(f"{destino.stem} ({n}){destino.suffix}")
        n += 1
    return cand


def converter_copia(arquivo, log=print, sem_avanco=300):
    """Grava, ao lado do original, uma COPIA pronta para o Premiere (H.264, keyframe a cada ~1 s, sem B-frames).
    O original nao e tocado. Mostra o andamento e devolve o caminho da copia."""
    import time
    arquivo = Path(arquivo)
    dur = _duracao_audio(arquivo)
    for velho in arquivo.parent.glob(".fc_tmp_prem_*"):
        _apagar_com_calma(velho)
    tmp = arquivo.with_name(f".fc_tmp_prem_{arquivo.name}")
    destino = nome_livre(arquivo.with_name(arquivo.stem + "_convertido.mp4"))
    g = str(_gop(arquivo))
    audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(arquivo)], capture_output=True, text=True).stdout.strip()
    a_cod = ["-c:a", "copy"] if audio in ("aac", "mp3", "") else ["-c:a", "aac", "-b:a", "320k"]
    base = ["-i", str(arquivo), "-map", "0:v:0", "-map", "0:a?", "-g", g, "-bf", "0", "-pix_fmt", "yuv420p"]
    fim = a_cod + ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
    cabeca = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    tentativas = []
    if _tem_nvenc():
        tentativas.append(cabeca + ["-hwaccel", "cuda"] + base + NV_ORIGINAL + fim)
        tentativas.append(cabeca + base + NV_ORIGINAL + fim)
    tentativas.append(cabeca + base + X264_ORIGINAL + fim)
    log(f"Convertendo para o Premiere ({mostrar_tempo(dur) if dur else 'duracao desconhecida'})...")
    erro = ""
    for cmd in tentativas:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        estado = {"ultimo": time.time(), "feito": 0.0}
        err_buf = []

        def ler(p=p, estado=estado):
            for linha in p.stdout:
                if linha.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        t = int(linha.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    if t > estado["feito"]:
                        estado["feito"] = t
                        estado["ultimo"] = time.time()

        def ler_erro(p=p, err_buf=err_buf):   # esvazia o stderr: se o buffer encher, o ffmpeg trava
            for linha in p.stderr:
                err_buf.append(linha)
                del err_buf[:-40]
        ths = [threading.Thread(target=ler, daemon=True), threading.Thread(target=ler_erro, daemon=True)]
        for th in ths:
            th.start()
        ultimo_pct = -10
        while p.poll() is None:
            time.sleep(1)
            checar()
            if dur:
                pct = int(min(100, estado["feito"] * 100 / dur))
                if pct >= ultimo_pct + 1:
                    ultimo_pct = pct
                    log(f"Convertendo... {pct}%")
            if time.time() - estado["ultimo"] > sem_avanco:
                p.kill()
                try:
                    p.wait(timeout=15)
                except Exception:  # noqa: BLE001
                    pass
                _apagar_com_calma(tmp)
                raise RuntimeError(f"a conversao ficou {sem_avanco} s sem avancar")
        for th in ths:
            th.join(timeout=2)
        checar()
        if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            break
        erro = "".join(err_buf).strip()[-200:]
        _apagar_com_calma(tmp)
    else:
        raise RuntimeError("nao consegui converter: " + erro)
    os.replace(tmp, destino)
    log(f"Copia para o Premiere pronta: {destino.name}")
    return destino


# ------------------------------------------------------------------ pipeline
# ------------------------------------------------------------- nomes dos arquivos
def slug(texto, limite=80):
    """Titulo -> Nome-Com-Tracos (mantem acentos; tira o que o Windows nao aceita, hashtags, links e emojis)."""
    import unicodedata
    texto = unicodedata.normalize("NFC", texto or "")
    texto = re.sub(r"https?://\S+|[#@]\w+", " ", texto)
    texto = "".join(c if (c.isalnum() or c in " -_") else " " for c in texto)
    partes = texto.split()
    saida = ""
    for p in partes:
        if len(saida) + len(p) + 1 > limite:
            break
        saida = f"{saida}-{p}" if saida else p
    return re.sub(r"-{2,}", "-", saida).strip("-_")


def descricao_util(texto):
    limpo = re.sub(r"https?://\S+|[#@]\w+", " ", texto or "")
    limpo = "".join(c for c in limpo if c.isalnum() or c.isspace())
    return len(limpo.split()) >= 4


def proximo_numero(pasta):
    """Maior numero FC_ ja usado na pasta + 1 (nunca repete, mesmo se algo for apagado)."""
    maior = 0
    for arq in Path(pasta).glob("FC_*"):
        m = re.match(r"FC_\d{2}_\d{2}_(\d{2,4})_", arq.name)
        if m:
            maior = max(maior, int(m.group(1)))
    return maior + 1


NUMEROS_RESERVADOS = {}   # pasta -> maior numero FC_ ja dado nesta sessao (arquivos ainda nao gravados)
NUMEROS_TRAVA = threading.Lock()


def reservar_numero(pasta):
    """Proximo numero FC_ livre. Dois videos processados ao mesmo tempo nunca ganham o mesmo numero, mesmo que
    os arquivos do primeiro ainda nao estejam na pasta."""
    chave = str(Path(pasta).resolve()).lower()
    with NUMEROS_TRAVA:
        n = max(proximo_numero(pasta), NUMEROS_RESERVADOS.get(chave, 0) + 1)
        NUMEROS_RESERVADOS[chave] = n
        return n


def gerar_titulo(descricao, texto, log):
    """Titulo curto em portugues, para quando ele nao veio junto com a traducao. Gemini, depois Ollama."""
    pedido = ("Crie um titulo curto em portugues do Brasil (no maximo 8 palavras, sem hashtags, sem emojis, "
              "sem aspas) que diga do que trata este video. Responda so com o titulo.\n\n"
              f"Descricao do post: {(descricao or '(sem descricao)')[:1500]}\n\nFala do video: {(texto or '')[:3000]}")
    if os.environ.get("GEMINI_API_KEY"):
        try:
            modelo = os.environ.get("GEMINI_MODEL") or GEMINI_PADRAO
            corpo = {"contents": [{"role": "user", "parts": [{"text": pedido}]}],
                     "generationConfig": {"temperature": 0.3, "maxOutputTokens": 60}}
            if ESTADO_GEMINI["thinking_off"]:
                corpo["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
            r = _gemini(f"/v1beta/models/{modelo}:generateContent", corpo, timeout=60)
            t = r["candidates"][0]["content"]["parts"][0]["text"].strip().splitlines()[0]
            if t.strip():
                return t
        except Exception as e:  # noqa: BLE001
            log(f"  titulo pelo Gemini falhou ({str(e)[:80]})")
    modelo = escolher_modelo_ollama()
    if modelo:
        try:
            r = _ollama("/api/chat", {"model": modelo, "stream": False, "think": False,
                                      "options": {"temperature": 0.3},
                                      "messages": [{"role": "user", "content": pedido}]}, timeout=120)
            t = r["message"]["content"].strip().splitlines()[0]
            if t.strip():
                return t
        except Exception as e:  # noqa: BLE001
            log(f"  titulo pelo Ollama falhou ({str(e)[:80]})")
    return ""


def processar(entrada, log=print, saida_base=None, legendar=True, destino="pt", trecho=None, estilo=None,
              estilo_v=None, audio=False, premiere=True, altura=None):
    """Baixa (e legenda) e salva direto na pasta escolhida:
       FC_MM_DD_NNN_Titulo.mp4 (original), FC_MM_DD_NNN_Titulo.srt e FC_MM_DD_NNN_Titulo_legendado.mp4.
    destino: idioma da legenda, "pt" (portugues do Brasil) ou "en" (ingles). Se o video ja esta nesse idioma,
    so transcreve; senao traduz. Arquivo do proprio computador: nao copia o original (so .srt e _legendado).
    audio: baixa so o som, em MP3 (sem legenda). premiere: se o original baixado nao estiver no formato que o
    Premiere le bem, prepara (converte) -- desligado, o original fica exatamente como veio do site.
    Devolve (arquivo principal, [todos os arquivos criados], original a preparar para o Premiere ou None)."""
    import shutil
    import tempfile
    import time
    legendar = legendar and not audio
    base = Path(saida_base) if saida_base else globals()["SAIDA_BASE"]
    log(f"Configuracao: {ENV_LIDO or 'nenhum .env encontrado'} | Gemini: "
        f"{'chave OK' if os.environ.get('GEMINI_API_KEY') else 'sem chave'}")
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fc_tmp_", dir=base))   # trabalho temporario dentro da propria pasta
    sessao = {"titulo": None}   # titulo sugerido pelo Gemini para ESTE video (videos em paralelo nao se misturam)
    gpu = Posse(VEZ_PROC, lambda: log("Esperando a vez..."))   # placa de video: um video por vez
    try:
        local = Path(entrada).is_file()
        if local:
            if legendar:
                gpu.pegar()
            video = Path(entrada).resolve()   # usa o arquivo no lugar: nada de copia
            meta = {"fonte": "arquivo", "titulo": Path(entrada).stem, "descricao": "", "perfil": "", "id": ""}
            if trecho:   # trecho de um arquivo do computador: o recorte vira um arquivo novo
                log(f"Recortando o trecho {mostrar_tempo(trecho[0])} a {mostrar_tempo(trecho[1])}...")
                cortar(video, tmp / "original.mp4", trecho[0], trecho[1], log)
                video, local = tmp / "original.mp4", False
        else:
            alvo_baixa = "o audio" if audio else "o video"
            if trecho:
                log(f"Baixando {alvo_baixa} (so o trecho {mostrar_tempo(trecho[0])} a {mostrar_tempo(trecho[1])})...")
            else:
                log(f"Baixando {alvo_baixa}...")
            with VEZ_DOWNLOAD():
                t_baixa = time.time()
                video, meta = baixar(entrada, tmp, log, trecho, audio=audio, altura=altura)
            video = Path(video)
            try:   # ajuda a entender downloads lentos: tempo e tamanho
                mb = video.stat().st_size / 1e6
                seg = max(1, int(time.time() - t_baixa))
                log(f"  download concluido em {seg} s ({mb:.0f} MB, {mb / seg:.1f} MB/s)")
            except OSError:
                pass
            if legendar:   # ja baixou: o proximo video pode comecar a baixar enquanto este e processado
                gpu.pegar()
            if audio:
                # o site pode ter entregue o video inteiro (Instagram pela pagina) ou o trecho nao saiu direto:
                # o MP3 e feito/recortado aqui
                cortar_aqui = trecho if trecho and _duracao_audio(video) > (trecho[1] - trecho[0]) + 5 else None
                if video.suffix.lower() != ".mp3" or cortar_aqui:
                    log("Convertendo para MP3...")
                    mp3 = extrair_mp3(video, tmp / "audio_final.mp3", cortar_aqui)
                    video.unlink(missing_ok=True)
                    video = mp3.rename(tmp / "original.mp3")
            elif trecho and duracao_e_altura(video)[0] > (trecho[1] - trecho[0]) + 5:
                # o site entregou o video inteiro (ex.: Instagram pela pagina): recorta aqui
                log("Recortando o trecho...")
                cortar(video, tmp / "trecho.mp4", trecho[0], trecho[1], log)
                video.unlink(missing_ok=True)
                video = (tmp / "trecho.mp4").rename(tmp / "original.mp4")

        # titulo: YouTube (e arquivo local) usam o proprio titulo; X/Instagram ganham um titulo gerado
        titulo = meta["titulo"] if meta["fonte"] in ("youtube", "arquivo", "outro") else ""
        reserva = "-".join(x for x in (meta.get("perfil"), meta.get("id")) if x) or "video"
        descricao = meta.get("descricao") if descricao_util(meta.get("descricao")) else ""

        # titulo + descricao do post: ajudam o Whisper e o tradutor a acertar nomes proprios
        contexto = "\n".join(x for x in ((meta.get("titulo") or "").replace("_", " "),
                                         (meta.get("descricao") or "")[:1200]) if x.strip())
        arquivos = []
        if legendar:
            analise = iniciar_analise_da_tela(video, duracao_e_altura(video)[0], log)   # em paralelo com o Whisper
            segs, idioma = transcrever(video, log, contexto)
            trechos = em_trechos(segs)
            if not trechos:
                raise RuntimeError("Nao encontrei fala nesse video.")
            textos = [t for _, _, t in trechos]
            if idioma == destino:
                log(f"O video ja esta em {NOMES_IDIOMA[destino]}: so vou legendar.")
            else:
                log(f"Idioma detectado: {idioma}. Legenda em {NOMES_IDIOMA[destino]}.")
                gpu.soltar()   # a traducao e na internet: o proximo video ja pode usar a placa
                textos = traduzir(textos, idioma, log, descricao=None if titulo else descricao, destino=destino,
                                  contexto=contexto, sessao=sessao)
            if not titulo:
                titulo = sessao.get("titulo") or gerar_titulo(descricao, " ".join(textos), log)
        elif not titulo and descricao:
            log("Criando um titulo a partir da descricao...")
            titulo = gerar_titulo(descricao, "", log)

        nome = slug(titulo) or slug(reserva) or "video"
        if trecho:   # deixa claro que nao e o video inteiro
            nome = f"{nome}_{tempo_no_nome(trecho[0])}"
        if meta["fonte"] == "arquivo":   # video do computador: mantem o nome dele (so ganha _legendado no fim)
            nome = Path(entrada).stem + (f"_{tempo_no_nome(trecho[0])}" if trecho else "")
            sufixo_lg = "" if destino == "pt" else "_" + destino.upper()
            prefixo, n = nome, 1
            while any((base / f"{prefixo}{suf}").exists() for suf in
                      (f"_legendado{sufixo_lg}.mp4", ".srt") + (() if local else (".mp4",))):
                n += 1
                prefixo = f"{nome} ({n})"
        else:
            prefixo = f"FC_{time.strftime('%m_%d')}_{reservar_numero(base):03d}_{nome}"
        log(f"Video: {prefixo}")

        original = base / f"{prefixo}{'.mp3' if audio else '.mp4'}"
        if not legendar:
            if local:
                raise RuntimeError("Isso ja e um arquivo no seu computador. Para legendar, use \"Baixar e legendar\".")
            shutil.move(str(video), str(original))
            log(f"Pronto! {original}")
            return original, [str(original)], (original if (premiere and not audio) else None)

        _, larg, alt = duracao_e_altura(video)
        estilo = estilo_completo(estilo_v if (estilo_v and alt > larg) else estilo)   # estilo do video vertical
        fs, maxc = medidas(larg, alt, estilo["tamanho"])
        legendas = montar_legendas(trechos, textos, maxc)
        escrever_srt(legendas, tmp / "legenda.srt")
        caixas = caixas_por_legenda(analise, legendas, log)
        salvar_analise(analise, Path(entrada).resolve() if local else original, duracao_e_altura(video)[0])
        posicoes = escolher_posicoes(legendas, caixas, larg, alt, fs, estilo["altura"])
        if any(caixas):
            log(f"  texto ja existente encontrado em {sum(1 for c in caixas if c)} de {len(legendas)} legendas; "
                "posicao ajustada onde precisava.")
        escrever_ass(legendas, posicoes, tmp / "legenda.ass", larg, alt, fs, estilo)
        gpu.pegar()   # de volta para a placa de video (se outro video estiver nela, espera a vez)
        # video baixado que nao esta bom para o Premiere: o original e preparado na mesma passada da legenda
        prep = None
        if not local and premiere:
            motivo = motivo_para_converter(video)
            if motivo:
                prep = tmp / "original_premiere.mp4"
                log(f"  o original tambem sera preparado para o Premiere ({motivo}), na mesma passada.")
        log("Colocando a legenda no video...")
        queimar(video, tmp / "legenda.ass", tmp / "legendado.mp4", log, extra=prep)
        if prep:
            video.unlink(missing_ok=True)
            video = prep

        legendado = base / f"{prefixo}_legendado{'' if destino == 'pt' else '_' + destino.upper()}.mp4"
        srt = base / f"{prefixo}.srt"   # mesmo nome do original: players e o Premiere acham a legenda sozinhos
        if not local:
            shutil.move(str(video), str(original))
        shutil.move(str(tmp / "legenda.srt"), str(srt))
        shutil.move(str(tmp / "legendado.mp4"), str(legendado))
        arquivos = ([] if local else [str(original)]) + [str(srt), str(legendado)]
        log(f"Pronto! {legendado}")
        return legendado, arquivos, None   # o original ja saiu pronto para o Premiere
    finally:
        gpu.soltar()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ refazer a legenda
def srt_livre(video, codigo):
    """<video>_<idioma>.srt ao lado do video (se existir, acrescenta (2), (3)...)."""
    video = Path(video)
    return nome_livre(video.with_name(f"{video.stem}_{codigo}.srt"))


def transcrever_para_srt(video, log):
    """Video do computador -> .srt no idioma da fala. Devolve (caminho_do_srt, codigo_do_idioma)."""
    video = Path(video)
    log(f"Video: {video.name}")
    with VEZ_PROC(lambda: log("Esperando a vez...")):
        segs, idioma = transcrever(video, log, video.stem.replace("_", " "))
    trechos = em_trechos(segs)
    if not trechos:
        raise RuntimeError("Nao encontrei fala nesse video.")
    _, larg, alt = duracao_e_altura(video)
    _, maxc = medidas(larg, alt, 100)
    destino = srt_livre(video, idioma)
    escrever_srt(montar_legendas(trechos, [t for _, _, t in trechos], maxc), destino)
    log(f"Idioma detectado: {idioma}. Legenda salva: {destino.name}")
    return destino, idioma


def traduzir_srt(srt, codigo_origem, codigo_destino, log, video=None):
    """Le um .srt (com as correcoes que voce fez a mao), traduz e grava <nome>_<idioma>.srt ao lado.
    Os tempos de cada legenda sao mantidos."""
    srt = Path(srt)
    cues = ler_srt(srt)
    if not cues:
        raise RuntimeError("O .srt esta vazio.")
    if codigo_origem == codigo_destino:
        raise RuntimeError("O video ja esta nesse idioma.")
    textos = [t for _, _, t in cues]
    log(f"Traduzindo {len(textos)} legendas para {LINGUAS.get(codigo_destino, (codigo_destino,))[0]}...")
    if codigo_destino in ("pt", "en"):
        novos = traduzir(textos, codigo_origem, log, destino=codigo_destino)
    else:
        novos = None
        if os.environ.get("GEMINI_API_KEY"):
            try:
                novos = traduzir_gemini(textos, codigo_origem, log, destino=codigo_destino)
            except Exception as e:  # noqa: BLE001
                checar()
                log(f"Gemini falhou: {str(e)[:200]}")
        if novos is None:
            log("Usando o tradutor do Google (qualidade menor).")
            novos = traduzir_google(textos, codigo_origem, "zh-CN" if codigo_destino == "zh" else codigo_destino)
    maxc = 42
    if video and Path(video).is_file():
        _, larg, alt = duracao_e_altura(video)
        maxc = medidas(larg, alt, 100)[1]
    destino = nome_livre(srt.with_name(f"{Path(video).stem if video else srt.stem}_{codigo_destino}.srt"))
    escrever_srt(montar_legendas(cues, novos, maxc), destino)
    log(f"Traducao salva: {destino.name}")
    return destino


def ler_srt(caminho):
    """[(inicio, fim, texto)] de um .srt (aceita o que voce editar a mao)."""
    txt = Path(caminho).read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    def seg(h, m, s_, ms):
        return int(h) * 3600 + int(m) * 60 + int(s_) + int(ms) / 1000
    cues = []
    for bloco in re.split(r"\n\s*\n", txt.strip()):
        linhas = bloco.strip().split("\n")
        for i, l in enumerate(linhas):
            m = re.match(r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", l)
            if m:
                texto = " ".join(x.strip() for x in linhas[i + 1:] if x.strip())
                texto = re.sub(r"<[^>]+>|\{[^}]*\}", "", texto).strip()
                if texto:
                    cues.append((seg(*m.groups()[:4]), seg(*m.groups()[4:]), texto))
                break
    return cues


def arquivos_para_refazer(arquivos, link=None, trecho=None):
    """(original, srt, legendado) de um video da lista, ou None se faltar algo."""
    existentes = [Path(a) for a in arquivos or [] if Path(a).exists()]
    srt = next((a for a in existentes if a.suffix.lower() == ".srt"), None)
    legendado = next((a for a in arquivos or [] if "_legendado" in Path(a).name), None)
    original = next((a for a in existentes if a.suffix.lower() == ".mp4" and "_legendado" not in a.name), None)
    if original is None and link and not trecho and Path(link).is_file():
        original = Path(link)   # video do proprio computador (o original fica onde estava)
    if not (srt and original and legendado):
        return None
    return original, srt, Path(legendado)


def refazer_legenda(arquivos, estilo, log=print, link=None, trecho=None, estilo_v=None):
    """Grava de novo o video legendado com o estilo pedido, usando o original e o .srt que ja estao na pasta
    (sem baixar, transcrever nem traduzir de novo). Edicoes feitas a mao no .srt entram."""
    import shutil
    import tempfile
    achados = arquivos_para_refazer(arquivos, link, trecho)
    if not achados:
        raise RuntimeError("Para refazer a legenda preciso do video original e do .srt desse video na pasta.")
    original, srt, legendado = achados
    cues = ler_srt(srt)
    if not cues:
        raise RuntimeError(f"O arquivo {srt.name} esta vazio ou nao e um .srt valido.")
    tmp = Path(tempfile.mkdtemp(prefix=".fc_tmp_", dir=legendado.parent))
    try:
        dur, larg, alt = duracao_e_altura(original)
        estilo = estilo_completo(estilo_v if (estilo_v and alt > larg) else estilo)
        log("Refazendo a legenda com o estilo novo...")
        analise = carregar_analise(original, dur)
        if analise:
            log("  usando a analise da tela que ja foi feita (sem chamar o Gemini de novo).")
        else:
            analise = iniciar_analise_da_tela(original, dur, log)
        fs, maxc = medidas(larg, alt, estilo["tamanho"])
        legendas = montar_legendas(cues, [t for _, _, t in cues], maxc)
        caixas = caixas_por_legenda(analise, legendas, log)
        salvar_analise(analise, original, dur)   # (no-op se ja veio do cache)
        posicoes = escolher_posicoes(legendas, caixas, larg, alt, fs, estilo["altura"])
        escrever_ass(legendas, posicoes, tmp / "legenda.ass", larg, alt, fs, estilo)
        log("Colocando a legenda no video...")
        queimar(original, tmp / "legenda.ass", tmp / "legendado.mp4", log)
        try:
            os.replace(tmp / "legendado.mp4", legendado)
        except PermissionError:
            raise RuntimeError(f"{legendado.name} esta aberto em outro programa (Premiere ou player). "
                               "Feche e tente de novo.") from None
        log(f"Pronto! {legendado}")
        return legendado
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- janela
ICONE_ICO = next((PASTA_APP / n for n in ("flowcaptions-v2.ico", "flowcaptions.ico", "fc-icone-v2.ico") if (PASTA_APP / n).exists()),
                 PASTA_APP / "flowcaptions.ico")   # nome novo: o Windows guarda icones em cache pelo nome do arquivo
ICONE_PNG = next((PASTA_APP / n for n in ("flowcaptions-v2.png", "flowcaptions.png", "fc-icone-v2.png") if (PASTA_APP / n).exists()),
                 PASTA_APP / "flowcaptions.png")
ARQ_LOG = PASTA_APP / "flowcaptions.log"


APP_ID = "FlowCaptions.App.1"


def _criar_atalho(caminho, alvo, args, pasta, icone):
    """Cria um atalho que carrega o mesmo ID da janela: assim, ao fixar na barra de tarefas,
    o Windows usa este atalho (com o icone do FlowCaption) e nao o pythonw/.bat."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell
    lnk = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    lnk.SetPath(str(alvo))
    lnk.SetArguments(args)
    lnk.SetWorkingDirectory(str(pasta))
    lnk.SetIconLocation(str(icone), 0)
    lnk.SetDescription(f"{NOME_APP} - videos legendados")
    loja = lnk.QueryInterface(propsys.IID_IPropertyStore)
    loja.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    loja.Commit()
    lnk.QueryInterface(pythoncom.IID_IPersistFile).Save(str(caminho), 0)


def criar_atalho_desktop():
    """Cria/atualiza os atalhos FlowCaption (Area de Trabalho e Menu Iniciar) com o icone atual."""
    if sys.platform != "win32":
        return
    try:
        try:
            import win32com  # noqa: F401
        except ImportError:   # instala uma vez o componente do Windows para Python
            gravar_log("Instalando pywin32 para os atalhos...")
            subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "-q",
                            "pywin32"], capture_output=True, timeout=300, creationflags=0x08000000)
            import site
            import importlib
            importlib.invalidate_caches()
            for p in site.getsitepackages():
                for sub in ("win32", "win32/lib", "pywin32_system32"):
                    alvo = Path(p) / sub
                    if alvo.is_dir() and str(alvo) not in sys.path:
                        sys.path.append(str(alvo))
                        if sub == "pywin32_system32":
                            os.add_dll_directory(str(alvo))
        pythonw = PASTA_APP / "python" / "pythonw.exe"
        args = f'"{PASTA_APP / "legendar.py"}"'
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        try:
            from win32com.shell import shell, shellcon
            desktop = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
        except Exception:  # noqa: BLE001
            pass
        menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for velho in ("FlameCaptions.lnk", "FlameCaptions v2.lnk", "FlameCaptions v3.lnk", "FlowCaptions.lnk"):   # nomes antigos
            for pasta in (desktop, menu):
                try:
                    (pasta / velho).unlink(missing_ok=True)
                except OSError:
                    pass
        for destino in (desktop / f"{NOME_APP}.lnk", menu / f"{NOME_APP}.lnk"):
            try:
                _criar_atalho(destino, pythonw, args, PASTA_APP, ICONE_ICO)
            except Exception as e:  # noqa: BLE001
                gravar_log(f"atalho {destino}: {e}")
        import ctypes   # avisa o Windows para redesenhar os icones
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception as e:  # noqa: BLE001
        gravar_log(f"atalhos: {e}")


def _numeros_versao(txt):
    return tuple(int(n) for n in re.findall(r"\d+", str(txt))[:4])


def buscar_atualizacao():
    """Pergunta ao GitHub se ha versao nova. Devolve None ou {"versao", "notas", "url"} (url do legendar.py da release)."""
    if not REPOSITORIO:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{REPOSITORIO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": NOME_APP})
    with urllib.request.urlopen(req, timeout=10) as resp:
        rel = json.loads(resp.read().decode("utf-8"))
    tag = rel.get("tag_name") or ""
    if not tag or _numeros_versao(tag) <= _numeros_versao(VERSAO):
        return None
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == "legendar.py"), None)
    if not asset:
        return None
    return {"versao": tag.lstrip("vV"), "notas": (rel.get("body") or "").strip(), "url": asset["browser_download_url"]}


def instalar_atualizacao(info):
    """Baixa o legendar.py novo, confere que e valido e troca (guardando o antigo em legendar.py.bak)."""
    if not info["url"].startswith("https://github.com/"):
        raise RuntimeError("endereco da atualizacao inesperado")
    req = urllib.request.Request(info["url"], headers={"User-Agent": NOME_APP})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dado = resp.read()
    if len(dado) < 50_000 or b"NOME_APP" not in dado:
        raise RuntimeError("o arquivo baixado nao parece o programa")
    compile(dado, "legendar.py", "exec")   # erro de sintaxe = nao instala
    alvo = Path(__file__).resolve()
    novo = alvo.with_name("legendar.py.novo")
    novo.write_bytes(dado)
    try:
        shutil.copy2(alvo, alvo.with_name("legendar.py.bak"))
        os.replace(novo, alvo)
    finally:
        novo.unlink(missing_ok=True)


def gravar_log(msg):
    try:
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def apagar_arquivos(arquivos, base):
    """Apaga so os arquivos FC_ de UM download, e so dentro da pasta de destino."""
    base = Path(base).resolve()
    for a in arquivos:
        a = Path(a).resolve()
        nosso = a.name.startswith("FC_") or a.suffix.lower() == ".srt" or "_legendado" in a.stem
        if a.parent != base or not nosso:
            raise RuntimeError(f"{a.name}: fora da pasta de destino ou nao e do FlowCaption; nao apaguei")
    for a in arquivos:
        Path(a).unlink(missing_ok=True)


def apagar_video(pasta, base):
    """Apaga a pasta de UM video (original, legenda e legendado). Recusa qualquer outra pasta."""
    import shutil
    pasta, base = Path(pasta).resolve(), Path(base).resolve()
    if not pasta.is_dir():
        return
    if pasta == base or base not in pasta.parents:
        raise RuntimeError("pasta fora da pasta de destino; nao apaguei por seguranca")
    conhecidos = {"original.mp4", "legendado.mp4", "legenda_pt.srt", "legenda_pt.ass"}
    if not any((pasta / n).exists() for n in conhecidos):
        raise RuntimeError("a pasta nao parece ser de um video do FlowCaption; nao apaguei")
    shutil.rmtree(pasta)


# cores do modo escuro (combinando com o icone)
COR = {"fundo": "#1e1e1e", "painel": "#232323", "linha": "#262626", "linha2": "#323232", "borda": "#3a3a3a",
       "sel": "#3e3e3e", "campo": "#151515", "texto": "#dcdcdc", "fraco": "#8e8e8e", "apagado": "#565656",
       "destaque": "#2d7ff9", "destaque2": "#5197ff", "ok": "#62b27c", "erro": "#e5534b", "contorno": "#5a5a5a"}
ESC = {"s": 1.0}   # escala da tela (125%, 150%...): tudo que e desenhado e multiplicado por isso


def P(v):
    """Pixels 'logicos' -> pixels reais da tela."""
    return int(round(v * ESC["s"]))


def janela():
    import queue
    import tempfile
    import threading
    import time
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont

    if sys.platform == "win32":
        try:   # barra de tarefas com o icone do FlowCaption, nao o do Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=criar_atalho_desktop, daemon=True).start()

    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)   # nitido em 125%/150%
            except Exception:  # noqa: BLE001
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    eventos = queue.Queue()
    trabalhos = queue.Queue()
    root = tk.Tk()
    def erro_na_janela(tipo, valor, tb):
        import traceback
        gravar_log("ERRO NA JANELA: " + "".join(traceback.format_exception(tipo, valor, tb))[-1500:])
    root.report_callback_exception = erro_na_janela
    if os.environ.get("FC_ESCALA"):   # so para testes
        ESC["s"] = float(os.environ["FC_ESCALA"])
        root.tk.call("tk", "scaling", 96 * ESC["s"] / 72)
    else:
        ESC["s"] = max(1.0, root.winfo_fpixels("1i") / 96)

    class Tela(tk.Canvas):
        """Canvas em pixels 'logicos': coordenadas e larguras sao multiplicadas pela escala da tela."""
        def __init__(self, master=None, **kw):
            for k in ("width", "height"):
                if k in kw:
                    kw[k] = P(kw[k])
            super().__init__(master, **kw)

        @staticmethod
        def _c(args):
            if len(args) == 1 and isinstance(args[0], (list, tuple)):
                args = args[0]
            return [v * ESC["s"] for v in args]

        @staticmethod
        def _k(kw):
            for k in ("width", "height"):
                if isinstance(kw.get(k), (int, float)):
                    kw[k] = max(1, kw[k] * ESC["s"])
            return kw

        def create_line(self, *a, **k):
            return super().create_line(*self._c(a), **self._k(k))

        def create_polygon(self, *a, **k):
            return super().create_polygon(*self._c(a), **self._k(k))

        def create_rectangle(self, *a, **k):
            return super().create_rectangle(*self._c(a), **self._k(k))

        def create_oval(self, *a, **k):
            return super().create_oval(*self._c(a), **self._k(k))

        def create_arc(self, *a, **k):
            return super().create_arc(*self._c(a), **self._k(k))

        def create_text(self, *a, **k):
            return super().create_text(*self._c(a), **self._k(k))

        def create_window(self, *a, **k):
            return super().create_window(*self._c(a), **self._k(k))

        def create_image(self, *a, **k):
            return super().create_image(*self._c(a), **k)

        def coords(self, item, *a):
            if a:
                return super().coords(item, *self._c(a))
            return [v / ESC["s"] for v in super().coords(item)]

        def bbox(self, *a):
            b = super().bbox(*a)
            return None if b is None else tuple(v / ESC["s"] for v in b)

        def winfo_width(self):
            return super().winfo_width() / ESC["s"]

        def winfo_height(self):
            return super().winfo_height() / ESC["s"]

    def med(fonte, texto):
        return fonte.measure(texto) / ESC["s"]
    root.title(NOME_APP)

    def area_util():
        """Retangulo da tela sem a barra de tarefas (esq, topo, dir, base); varios monitores: a tela inteira."""
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                r = wintypes.RECT()
                ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)   # area de trabalho principal
                return r.left, r.top, r.right, r.bottom
            except Exception:  # noqa: BLE001
                pass
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - P(48)

    def telas_visiveis():
        """Retangulo que cobre todos os monitores (para saber se uma posicao salva ainda existe)."""
        if sys.platform == "win32":
            try:
                import ctypes
                u = ctypes.windll.user32
                x, y, w, h = (u.GetSystemMetrics(k) for k in (76, 77, 78, 79))
                if w > 0 and h > 0:
                    return x, y, x + w, y + h
            except Exception:  # noqa: BLE001
                pass
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()

    def posicionar_janela():
        e0, t0, d0, b0 = area_util()
        livre_l, livre_a = d0 - e0, b0 - t0
        min_l, min_a = min(P(880), livre_l - 20), min(P(600), livre_a - 20)
        root.minsize(min_l, min_a)
        salvo = ler_config().get("janela") or {}
        larg = int(salvo.get("w") or P(1000))
        alt = int(salvo.get("h") or P(840))
        larg, alt = max(min_l, min(larg, livre_l)), max(min_a, min(alt, livre_a))   # nunca maior que a tela util
        x, y = salvo.get("x"), salvo.get("y")
        vx0, vy0, vx1, vy1 = telas_visiveis()
        ok = (isinstance(x, int) and isinstance(y, int) and x + larg > vx0 + 80 and x < vx1 - 80
              and y >= vy0 - 5 and y < vy1 - 80)
        if ok:   # posicao salva: so corrige se a janela passa da area util do monitor principal
            if e0 <= x < d0:
                x = min(x, d0 - larg)
            if t0 <= y < b0:
                y = min(y, b0 - alt)
            y = max(y, vy0)
        else:    # primeira vez (ou o monitor sumiu): centraliza na area util
            x, y = e0 + (livre_l - larg) // 2, t0 + (livre_a - alt) // 2
        root.geometry(f"{larg}x{alt}+{x}+{y}")
        if salvo.get("max"):
            root.after(50, lambda: root.state("zoomed"))

    def guardar_janela():
        try:
            if root.state() == "iconic":
                return
            if root.state() == "zoomed":
                salvar_config(janela={**(ler_config().get("janela") or {}), "max": True})
                return
            salvar_config(janela={"x": root.winfo_x(), "y": root.winfo_y(), "w": root.winfo_width(),
                                  "h": root.winfo_height(), "max": False})
        except Exception:  # noqa: BLE001
            pass
    posicionar_janela()
    root.configure(bg=COR["fundo"])
    try:
        if sys.platform == "win32" and ICONE_ICO.exists():
            root.iconbitmap(default=str(ICONE_ICO))
        if ICONE_PNG.exists():
            root._icone = tk.PhotoImage(file=str(ICONE_PNG))
            root.iconphoto(True, root._icone)
    except Exception:  # noqa: BLE001
        pass

    def barra_titulo_escura(tentativa=0):
        """Barra de titulo do Windows na cor do programa (escura), em vez da branca padrao."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi

            def bgr(h):
                return int(h[1:3], 16) | int(h[3:5], 16) << 8 | int(h[5:7], 16) << 16
            filho = root.winfo_id()
            resultados = []
            for hwnd in {filho, user32.GetParent(filho), user32.GetAncestor(filho, 2)}:
                if not hwnd:
                    continue
                # 20/19: modo escuro | 35: cor da barra | 36: cor do texto | 34: cor da borda (Windows 11)
                for attr, val in ((20, 1), (19, 1), (35, bgr(COR["fundo"])), (36, bgr(COR["texto"])),
                                  (34, bgr(COR["contorno"]))):   # 34: linha fina em volta da janela
                    v = ctypes.c_int(val)
                    resultados.append(dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)))
                user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)   # redesenha a moldura
            if tentativa == 0:
                gravar_log(f"barra de titulo escura: codigos {resultados}")
            if tentativa >= 1500 and not any(r == 0 for r in resultados[4::5]):   # sem contorno do sistema (Windows 10)
                root.configure(highlightthickness=1, highlightbackground=COR["contorno"], highlightcolor=COR["contorno"])
        except Exception as e:  # noqa: BLE001
            gravar_log(f"barra de titulo escura falhou: {e}")
    root.update_idletasks()
    for ms in (0, 120, 500, 1500):   # repete: a moldura so aceita depois que a janela aparece
        root.after(ms, lambda m=ms: barra_titulo_escura(m))

    f_texto = tkfont.Font(family="Segoe UI", size=10)
    f_negrito = tkfont.Font(family="Segoe UI Semibold", size=10)
    f_titulo = tkfont.Font(family="Segoe UI Semibold", size=15)
    f_linha = tkfont.Font(family="Segoe UI Semibold", size=11)
    f_status = tkfont.Font(family="Segoe UI", size=9)
    f_secao = tkfont.Font(family="Segoe UI Semibold", size=9)
    f_tag = tkfont.Font(family="Segoe UI Semibold", size=8)

    def retangulo(c, x0, y0, x1, y1, r, **kw):
        """Retangulo de canto reto, como no Premiere (o Tk nao suaviza cantos arredondados)."""
        return c.create_rectangle(x0, y0, x1, y1, **kw)

    class Botao(Tela):
        """Botao arredondado com efeito ao passar o mouse."""
        def __init__(self, pai, texto, cmd, primario=False, bg=None):
            self.largura = med(f_negrito, texto) + 32
            super().__init__(pai, width=self.largura, height=34, bg=bg or COR["fundo"], highlightthickness=0, bd=0,
                             cursor="hand2")
            self.normal = COR["destaque"] if primario else COR["linha2"]
            self.hover = COR["destaque2"] if primario else COR["sel"]
            self.primario, self.ativo = primario, True
            self.forma = retangulo(self, 0, 0, self.largura, 34, 0, fill=self.normal,
                                   outline="" if primario else COR["borda"])
            self.rotulo = self.create_text(self.largura / 2, 17, text=texto, font=f_negrito,
                                           fill="#ffffff" if primario else COR["texto"])
            self.bind("<Enter>", lambda e: self.ativo and self.itemconfig(self.forma, fill=self.hover))
            self.bind("<Leave>", lambda e: self.ativo and self.itemconfig(self.forma, fill=self.normal))
            self.bind("<Button-1>", lambda e: self.ativo and cmd())

        def ativar(self, sim):
            """Liga/desliga o botao (desligado fica apagado e nao responde)."""
            self.ativo = sim
            self.configure(cursor="hand2" if sim else "")
            self.itemconfig(self.forma, fill=self.normal if sim else COR["linha"],
                            outline=("" if self.primario else COR["borda"]) if sim else COR["borda"])
            self.itemconfig(self.rotulo, fill=("#ffffff" if self.primario else COR["texto"]) if sim else COR["apagado"])

    # ---- cabecalho
    ui_fila = queue.Queue()   # funcoes que as threads pedem para rodar na janela
    estilo_ttk = ttk.Style(root)
    try:
        estilo_ttk.theme_use("clam")
    except tk.TclError:
        pass
    estilo_ttk.configure("FC.TCombobox", fieldbackground=COR["campo"], background=COR["linha2"], foreground=COR["texto"],
                         arrowcolor=COR["texto"], bordercolor=COR["borda"], lightcolor=COR["campo"],
                         darkcolor=COR["campo"], selectbackground=COR["campo"], selectforeground=COR["texto"],
                         insertcolor=COR["texto"], padding=6)
    estilo_ttk.map("FC.TCombobox", fieldbackground=[("readonly", COR["campo"])], foreground=[("readonly", COR["texto"])],
                   selectbackground=[("readonly", COR["campo"])])
    estilo_ttk.configure("FC.Horizontal.TScale", troughcolor=COR["campo"], background=COR["destaque"],
                         bordercolor=COR["borda"], lightcolor=COR["borda"], darkcolor=COR["borda"],
                         gripcount=0, sliderthickness=P(14), sliderlength=P(14), troughrelief="flat")
    estilo_ttk.map("FC.Horizontal.TScale", background=[("active", COR["destaque2"])])
    root.option_add("*TCombobox*Listbox.background", COR["painel"])
    root.option_add("*TCombobox*Listbox.foreground", COR["texto"])
    root.option_add("*TCombobox*Listbox.selectBackground", COR["destaque"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

    topo = tk.Frame(root, bg=COR["fundo"])
    topo.pack(fill="x", padx=P(24), pady=(P(20), P(0)))
    cab = tk.Frame(topo, bg=COR["fundo"])
    cab.pack(fill="x")
    try:
        if ICONE_PNG.exists():
            logo = tk.PhotoImage(file=str(ICONE_PNG))
            logo = logo.subsample(max(1, round(logo.width() / P(40))))
            root._logo = logo
            tk.Label(cab, image=logo, bg=COR["fundo"]).pack(side="left", padx=(P(0), P(12)))
    except Exception:  # noqa: BLE001
        pass
    textos = tk.Frame(cab, bg=COR["fundo"])
    textos.pack(side="left")
    tk.Label(textos, text=NOME_APP, font=f_titulo, bg=COR["fundo"], fg=COR["texto"]).pack(anchor="w")
    tk.Label(textos, text="Baixe e legende vídeos da internet ou do seu computador", font=f_texto,
             bg=COR["fundo"], fg=COR["fraco"]).pack(anchor="w")
    link_config = tk.Label(cab, text="Chave e cookies", font=f_negrito, bg=COR["fundo"], fg=COR["destaque2"],
                           cursor="hand2")
    link_config.pack(side="right", anchor="n")
    link_config.bind("<Button-1>", lambda e: abrir_configuracao(False))
    link_atualizar = tk.Label(cab, text="", font=f_negrito, bg=COR["fundo"], fg=COR["ok"], cursor="hand2")

    def oferecer_atualizacao(info):
        link_atualizar.configure(text=f"Nova versão {info['versao']} · atualizar")
        link_atualizar.pack(side="right", anchor="n", padx=(0, P(18)))

        def clicar(_e=None):
            notas = info["notas"][:600] + ("..." if len(info["notas"]) > 600 else "")
            if not confirmar(f"Atualizar o {NOME_APP} da versão {VERSAO} para a {info['versao']}?"
                             + (f"\n\nNovidades:\n{notas}" if notas else "")
                             + "\n\nO programa vai fechar e abrir de novo. Downloads em andamento serão interrompidos.",
                             chave="atualizar-" + info["versao"]):
                return
            try:
                instalar_atualizacao(info)
            except Exception as e:  # noqa: BLE001
                gravar_log(f"ERRO atualizacao: {e}")
                messagebox.showerror(NOME_APP, "Não consegui atualizar:\n\n" + str(e)[:300])
                return
            try:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(PASTA_APP))
            finally:
                root.destroy()
        link_atualizar.bind("<Button-1>", clicar)

    def verificar_atualizacao():
        if not REPOSITORIO:
            return

        def rodar():
            try:
                info = buscar_atualizacao()
            except Exception as e:  # noqa: BLE001
                gravar_log(f"atualizacao nao verificada: {str(e)[:100]}")
                return
            if info:
                ui_fila.put(lambda: oferecer_atualizacao(info))
        threading.Thread(target=rodar, daemon=True).start()

    # ---- abas
    ABAS = (("baixar", "Baixar da internet"), ("trechos", "Achar trechos"), ("pc", "Do computador"),
            ("transcrever", "Transcrever"), ("estilo", "Estilo da legenda"))
    aba = {"v": "baixar"}
    barra_abas = Tela(topo, height=44, bg=COR["fundo"], highlightthickness=0, bd=0)
    barra_abas.pack(fill="x", pady=(P(16), P(0)))

    def _ocultos():
        return set(ler_config().get("avisos_ocultos") or [])

    def _dialogo(texto, botoes, chave):
        """Caixa escura com caixinha 'Nao mostrar esta mensagem novamente'. Devolve True se clicou no 1o botao."""
        res = {"ok": False}
        dlg = tk.Toplevel(root)
        dlg.title(NOME_APP)
        dlg.configure(bg=COR["fundo"])
        dlg.transient(root)
        dlg.resizable(False, False)
        tk.Label(dlg, text=texto, font=f_texto, bg=COR["fundo"], fg=COR["texto"], justify="left",
                 wraplength=P(440)).pack(padx=P(24), pady=(P(22), P(12)), anchor="w")
        marca = tk.BooleanVar()
        tk.Checkbutton(dlg, text="Não mostrar esta mensagem novamente", variable=marca, font=f_status,
                       bg=COR["fundo"], fg=COR["fraco"], selectcolor=COR["campo"], activebackground=COR["fundo"],
                       activeforeground=COR["texto"], highlightthickness=0, bd=0, cursor="hand2"
                       ).pack(padx=P(22), anchor="w")
        linha = tk.Frame(dlg, bg=COR["fundo"])
        linha.pack(fill="x", padx=P(24), pady=(P(16), P(20)))

        def fechar(ok):
            res["ok"] = ok
            if ok and marca.get():   # so guarda a escolha quando a resposta foi "sim"/"ok"
                salvar_config(avisos_ocultos=sorted(_ocultos() | {chave}))
                atualizar_link_avisos()
            dlg.destroy()
        for k, rot in reversed(list(enumerate(botoes))):
            Botao(linha, rot, lambda ok=(k == 0): fechar(ok), primario=(k == 0)).pack(side="right", padx=(P(8), P(0)))
        dlg.protocol("WM_DELETE_WINDOW", lambda: fechar(len(botoes) == 1))
        dlg.bind("<Escape>", lambda e: fechar(len(botoes) == 1))
        dlg.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dlg.winfo_reqheight()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()
        dlg.focus_set()
        root.wait_window(dlg)
        return res["ok"]

    # ---- configuracao: chave do Gemini e cookies (abre sozinha na primeira vez, sem chave)
    def abrir_configuracao(primeira=False):
        dlg = tk.Toplevel(root)
        dlg.title(NOME_APP + " - Configuração")
        dlg.configure(bg=COR["fundo"])
        dlg.transient(root)
        dlg.resizable(False, False)
        L = P(620)   # largura do texto

        def titulo(txt, obs):
            l = tk.Frame(dlg, bg=COR["fundo"])
            l.pack(fill="x", padx=P(26), pady=(P(18), P(0)))
            tk.Label(l, text=txt, font=f_negrito, bg=COR["fundo"], fg=COR["texto"]).pack(side="left")
            tk.Label(l, text=obs, font=f_status, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left", padx=(P(10), 0))

        def texto(txt, cor=None):
            return tk.Label(dlg, text=txt, font=f_texto, bg=COR["fundo"], fg=cor or COR["fraco"], justify="left",
                            anchor="w", wraplength=L)

        tk.Label(dlg, text="Configuração inicial" if primeira else "Chave do Gemini e cookies", font=f_titulo,
                 bg=COR["fundo"], fg=COR["texto"]).pack(anchor="w", padx=P(26), pady=(P(22), 0))
        texto("O programa precisa de duas coisas suas. Elas ficam só neste computador, na pasta do programa, e não "
              "vão para lugar nenhum além do Google e do site de onde você baixa.").pack(anchor="w", padx=P(26),
                                                                                         pady=(P(6), 0))

        # --- 1. Gemini
        titulo("1. Chave do Gemini", "necessária para traduzir legendas e achar trechos (grátis)")
        texto("a) Clique em \"Abrir página do Gemini\" e entre com a sua conta Google.\n"
              "b) Clique em \"Criar chave de API\" e copie a chave que aparecer.\n"
              "c) Cole a chave aqui embaixo e clique em \"Testar e salvar\".").pack(anchor="w", padx=P(26), pady=(P(6), 0))
        l1 = tk.Frame(dlg, bg=COR["fundo"])
        l1.pack(fill="x", padx=P(26), pady=(P(10), 0))
        Botao(l1, "Abrir página do Gemini", lambda: webbrowser.open("https://aistudio.google.com/apikey")).pack(side="left")
        l2 = tk.Frame(dlg, bg=COR["fundo"])
        l2.pack(fill="x", padx=P(26), pady=(P(10), 0))
        ent_chave = entrada(l2)
        ent_chave.configure(show="•")
        ent_chave.pack(side="left", fill="x", expand=True, ipady=P(8))
        msg_chave = tk.StringVar(value="Chave já salva neste computador." if os.environ.get("GEMINI_API_KEY") else "")
        lbl_chave = tk.Label(dlg, textvariable=msg_chave, font=f_status, bg=COR["fundo"], fg=COR["ok"], anchor="w",
                             justify="left", wraplength=L)

        def salvar_chave():
            chave = ent_chave.get().strip()
            if not chave:
                ent_chave.focus()
                return
            lbl_chave.configure(fg=COR["fraco"])
            msg_chave.set("Testando a chave...")
            dlg.update_idletasks()

            def rodar():
                ok, m = testar_chave_gemini(chave)

                def fim():
                    if not dlg.winfo_exists():
                        return
                    if ok:
                        try:
                            salvar_chave_gemini(chave)
                            ent_chave.delete(0, "end")
                            lbl_chave.configure(fg=COR["ok"])
                            msg_chave.set("Chave válida e salva ✓")
                        except OSError as e:
                            lbl_chave.configure(fg=COR["erro"])
                            msg_chave.set("A chave vale, mas não consegui salvar na pasta do programa: " + str(e)[:120])
                    else:
                        lbl_chave.configure(fg=COR["erro"])
                        msg_chave.set(m)
                ui_fila.put(fim)
            threading.Thread(target=rodar, daemon=True).start()
        Botao(l2, "Testar e salvar", salvar_chave, primario=True).pack(side="left", padx=(P(10), 0))
        ent_chave.bind("<Return>", lambda e: salvar_chave())
        lbl_chave.pack(anchor="w", padx=P(26), pady=(P(6), 0))

        # --- 2. cookies
        titulo("2. Cookies (login)", "opcional: Instagram, vídeos só para membros ou com restrição")
        texto("a) No Chrome, instale a extensão \"Get cookies.txt LOCALLY\" (botão abaixo).\n"
              "b) Abra o youtube.com já logado, clique na extensão e em \"Export\". Salve o arquivo na pasta do "
              "programa (botão abaixo). Para baixar do Instagram, faça o mesmo em instagram.com.\n"
              "Esses arquivos são o seu login: nunca envie para ninguém. Quando o site deixar de aceitar, exporte "
              "de novo.").pack(anchor="w", padx=P(26), pady=(P(6), 0))
        l3 = tk.Frame(dlg, bg=COR["fundo"])
        l3.pack(fill="x", padx=P(26), pady=(P(10), 0))
        Botao(l3, "Abrir página da extensão", lambda: webbrowser.open(
            "https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc")
              ).pack(side="left")

        def abrir_pasta_app():
            if sys.platform == "win32":
                os.startfile(str(PASTA_APP))   # noqa: S606
        Botao(l3, "Abrir pasta do programa", abrir_pasta_app).pack(side="left", padx=(P(10), 0))
        msg_ck = tk.StringVar()
        lbl_ck = tk.Label(dlg, textvariable=msg_ck, font=f_status, bg=COR["fundo"], fg=COR["fraco"], anchor="w",
                          justify="left")

        def verificar():
            partes = []
            for rot, site in (("YouTube", "youtube"), ("Instagram", "instagram")):
                arq = cookies_do_site(site)
                partes.append(f"{rot}: " + (f"encontrado ✓ ({arq.name})" if arq else "ainda não encontrado"))
            msg_ck.set("     ".join(partes))
        Botao(l3, "Verificar de novo", verificar).pack(side="left", padx=(P(10), 0))
        lbl_ck.pack(anchor="w", padx=P(26), pady=(P(8), 0))
        verificar()

        # --- fim
        fim_ = tk.Frame(dlg, bg=COR["fundo"])
        fim_.pack(fill="x", padx=P(26), pady=(P(22), P(20)))

        def concluir():
            if not os.environ.get("GEMINI_API_KEY") and not messagebox.askyesno(
                    NOME_APP, "Sem a chave do Gemini a tradução cai no Google Tradutor (qualidade menor) e a busca "
                              "de trechos não funciona.\n\nConcluir mesmo assim?", parent=dlg):
                return
            salvar_config(configurado=True)
            dlg.destroy()
        Botao(fim_, "Concluir", concluir, primario=True).pack(side="right")
        if primeira:
            Botao(fim_, "Fazer depois", dlg.destroy).pack(side="right", padx=(0, P(10)))
        else:
            Botao(fim_, "Fechar", dlg.destroy).pack(side="right", padx=(0, P(10)))
        dlg.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - dlg.winfo_reqwidth()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - dlg.winfo_reqheight()) // 4)
        dlg.geometry(f"+{x}+{y}")
        dlg.grab_set()
        ent_chave.focus()

    def aviso(texto, chave=None):
        """Mensagem informativa; some de vez se a pessoa marcar 'nao mostrar novamente'."""
        chave = chave or texto[:50]
        if chave in _ocultos():
            return
        _dialogo(texto, ("OK",), chave)

    def confirmar(texto, chave=None):
        """Pergunta Sim/Nao que nao apaga nada; com 'nao mostrar novamente' passa a responder Sim sozinha."""
        chave = chave or texto[:20]
        if chave in _ocultos():
            return True
        return _dialogo(texto, ("Sim", "Não"), chave)

    def desenhar_abas():
        c = barra_abas
        c.delete("all")
        larguras = [med(f_negrito, rot) + 32 for _, rot in ABAS]
        c.create_line(0, 43, 4000, 43, fill=COR["borda"])
        x = 0
        for (cod, rot), w in zip(ABAS, larguras):
            tag = f"aba_{cod}"
            ativo = aba["v"] == cod
            c.create_rectangle(x, 0, x + w, 42, fill=COR["fundo"], outline="", tags=(tag,))
            if ativo:
                c.create_rectangle(x, 41, x + w, 44, fill=COR["destaque"], outline="", tags=(tag,))
            c.create_text(x + w / 2, 21, text=rot, font=f_negrito,
                          fill="#ffffff" if ativo else COR["fraco"], tags=(tag,))
            c.tag_bind(tag, "<Button-1>", lambda e, k=cod: mostrar_aba(k))
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))
            x += w

    corpo = tk.Frame(root, bg=COR["fundo"])          # abas Baixar e Do computador: area + opcoes + lista
    area = tk.Frame(corpo, bg=COR["fundo"])
    area.pack(fill="x", padx=P(24))
    aba_baixar = tk.Frame(area, bg=COR["fundo"])
    aba_pc = tk.Frame(area, bg=COR["fundo"])
    opcoes = tk.Frame(corpo, bg=COR["fundo"])
    opcoes.pack(fill="x", padx=P(24))
    aba_estilo = tk.Frame(root, bg=COR["fundo"])
    aba_trans = tk.Frame(root, bg=COR["fundo"])      # aba Transcrever (tela inteira, sem a lista de videos)
    aba_trechos = tk.Frame(root, bg=COR["fundo"])    # aba Achar trechos (tela inteira)
    telas_cheias = {"estilo": aba_estilo, "transcrever": aba_trans, "trechos": aba_trechos}

    def mostrar_aba(k):
        aba["v"] = k
        desenhar_abas()
        if k in telas_cheias:
            corpo.pack_forget()
            for kk, f_ in telas_cheias.items():
                f_.pack_forget()
            telas_cheias[k].pack(fill="both", expand=True, padx=P(24), pady=(P(16), P(16)))
            if k == "estilo":
                listar_videos_previa()
                agendar_previa(0)
            elif k == "transcrever":
                campo_tr.focus()
            elif k == "trechos":
                campo_at.focus()
        else:
            for f_ in telas_cheias.values():
                f_.pack_forget()
            corpo.pack(fill="both", expand=True)
            aba_baixar.pack_forget()
            aba_pc.pack_forget()
            (aba_baixar if k == "baixar" else aba_pc).pack(fill="x")
            linha_pasta.pack_forget()   # "Salvar em" so vale para downloads; arquivo do PC fica na pasta dele
            if k == "baixar":
                linha_pasta.pack(fill="x", pady=(P(14), P(0)))
            (campo if k == "baixar" else campo_pc).focus()

    def entrada(pai, largura=None, fonte=("Segoe UI", 11), bg=None):
        e = tk.Entry(pai, font=fonte, bg=COR["campo"], fg=COR["texto"], insertbackground=COR["texto"], relief="flat",
                     highlightthickness=1, highlightbackground=COR["borda"], highlightcolor=COR["destaque"])
        if largura:
            e.configure(width=largura)
        return e

    def criar_opcao(pai, texto, ligado, ao_mudar):
        """Caixinha de marcar com texto (mesmo visual das caixas da lista). Devolve um dict:
        on (marcada?), canvas (para posicionar), marcar(bool), ativar(bool)."""
        o = {"on": bool(ligado), "ativa": True}
        c = Tela(pai, width=med(f_negrito, texto) + 34, height=28, bg=COR["fundo"], highlightthickness=0, bd=0,
                 cursor="hand2")

        def desenhar():
            c.delete("all")
            if o["on"]:
                cor = COR["destaque"] if o["ativa"] else COR["borda"]
                c.create_rectangle(2, 6, 18, 22, fill=cor, outline=cor)
                c.create_line(6, 14, 9, 18, 15, 10, fill="#ffffff", width=2, capstyle="round", joinstyle="round")
            else:
                c.create_rectangle(2, 6, 18, 22, fill=COR["campo"], outline=COR["fraco"] if o["ativa"] else COR["borda"])
            c.create_text(28, 14, text=texto, font=f_negrito, anchor="w",
                          fill=(COR["texto"] if o["on"] else COR["fraco"]) if o["ativa"] else COR["apagado"])

        def clicou(_e=None):
            if o["ativa"]:
                o["on"] = not o["on"]
                desenhar()
                ao_mudar(o["on"])

        def marcar(valor):
            o["on"] = bool(valor)
            desenhar()

        def ativar(sim):
            o["ativa"] = sim
            c.configure(cursor="hand2" if sim else "")
            desenhar()
        c.bind("<Button-1>", clicou)
        o.update(canvas=c, marcar=marcar, ativar=ativar)
        desenhar()
        return o

    def criar_trecho(pai, get_link, com_busca):
        """Chave 'So um trecho' + painel De/Ate (e, para links, a busca por assunto)."""
        t = {"on": False}
        linha = tk.Frame(pai, bg=COR["fundo"])
        linha.pack(fill="x", pady=(P(14), P(0)))
        chave = Tela(linha, width=150, height=30, bg=COR["fundo"], highlightthickness=0, bd=0, cursor="hand2")
        chave.pack(side="left")
        painel = tk.Frame(pai, bg=COR["painel"], padx=P(14), pady=P(12), highlightthickness=1,
                          highlightbackground=COR["borda"])

        def desenhar_chave():
            chave.delete("all")
            on = t["on"]
            chave.create_rectangle(2, 8, 36, 24, fill=COR["destaque"] if on else COR["linha2"],
                                   outline=COR["destaque"] if on else COR["borda"])
            chave.create_rectangle((22 if on else 5), 11, (33 if on else 16), 21, fill="#ffffff" if on else COR["fraco"],
                                   outline="")
            chave.create_text(46, 16, text="Só um trecho", font=f_negrito, fill=COR["texto"] if on else COR["fraco"],
                              anchor="w")

        def alternar():
            t["on"] = not t["on"]
            desenhar_chave()
            if t["on"]:
                painel.pack(fill="x", pady=(P(10), P(0)), after=linha)
                ent_ini.focus()
            else:
                painel.pack_forget()
        chave.bind("<Button-1>", lambda e: alternar())
        desenhar_chave()

        l1 = tk.Frame(painel, bg=COR["painel"])
        l1.pack(fill="x")
        tk.Label(l1, text="DE", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
        ent_ini = entrada(l1, 9)
        ent_ini.pack(side="left", padx=(P(8), P(16)), ipady=P(6))
        tk.Label(l1, text="ATÉ", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
        ent_fim = entrada(l1, 9)
        ent_fim.pack(side="left", padx=(P(8), P(16)), ipady=P(6))
        dur_var = tk.StringVar(value="ex.: 1:12:40")
        tk.Label(l1, textvariable=dur_var, font=f_status, bg=COR["painel"], fg=COR["apagado"]).pack(side="left")

        def atualizar_duracao(*_):
            a, b = ler_tempo(ent_ini.get()), ler_tempo(ent_fim.get())
            dur_var.set(f"duração: {mostrar_tempo(b - a)}" if a is not None and b is not None and b > a
                        else "ex.: 1:12:40")
        for e_ in (ent_ini, ent_fim):
            e_.bind("<KeyRelease>", atualizar_duracao)

        def usar_achado(a):
            for e_, v in ((ent_ini, a["inicio"]), (ent_fim, a["fim"])):
                e_.delete(0, "end")
                e_.insert(0, mostrar_tempo(v))
            atualizar_duracao()

        if com_busca:
            def ver_no_site():
                link = get_link()
                if link.startswith(("http://", "https://")):
                    webbrowser.open(link_no_tempo(link, ler_tempo(ent_ini.get()) or 0))
            Botao(l1, "Ver no navegador", ver_no_site, bg=COR["painel"]).pack(side="right")

            l2 = tk.Frame(painel, bg=COR["painel"])
            l2.pack(fill="x", pady=(P(12), P(0)))
            tk.Label(l2, text="PROCURAR ASSUNTO", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
            ent_assunto = entrada(l2)
            ent_assunto.pack(side="left", fill="x", expand=True, padx=(P(10), P(10)), ipady=P(6))
            busca_estado = tk.StringVar(value="Escreva o assunto (ex.: quando falam do Conor) e clique em Encontrar.")
            tk.Label(painel, textvariable=busca_estado, font=f_status, bg=COR["painel"], fg=COR["fraco"],
                     anchor="w", justify="left", wraplength=P(700)).pack(fill="x", pady=(P(8), P(0)))
            resultados = tk.Frame(painel, bg=COR["painel"])
            resultados.pack(fill="x")
            buscando = {"v": False}

            def mostrar_achados(achados):
                buscando["v"] = False
                for w in resultados.winfo_children():
                    w.destroy()
                if not achados:
                    busca_estado.set("Não encontrei esse assunto no vídeo. Tente com outras palavras.")
                    return
                busca_estado.set("Clique num trecho para usar (os tempos vão para De/Até). Confira com \"Ver no navegador\".")
                for a in achados:
                    c = tk.Frame(resultados, bg=COR["linha"], padx=P(12), pady=P(8), cursor="hand2", highlightthickness=1,
                                 highlightbackground=COR["borda"])
                    c.pack(fill="x", pady=(P(8), P(0)))
                    t1 = tk.Label(c, text=f"{mostrar_tempo(a['inicio'])} – {mostrar_tempo(a['fim'])}   ·   {a['resumo']}",
                                  font=f_negrito, bg=COR["linha"], fg=COR["texto"], anchor="w", justify="left",
                                  wraplength=P(680))
                    t1.pack(fill="x")
                    t2 = tk.Label(c, text=f"“{a['fala']}…”" if a.get("fala") else "", font=f_status,
                                  bg=COR["linha"], fg=COR["fraco"], anchor="w", justify="left", wraplength=P(680))
                    t2.pack(fill="x")
                    for w in (c, t1, t2):
                        w.bind("<Button-1>", lambda e, aa=a: usar_achado(aa))
                        w.bind("<Enter>", lambda e, cc=c: cc.configure(highlightbackground=COR["destaque2"]))
                        w.bind("<Leave>", lambda e, cc=c: cc.configure(highlightbackground=COR["borda"]))
                usar_achado(achados[0])

            def encontrar():
                link, assunto = get_link(), ent_assunto.get().strip()
                if buscando["v"]:
                    return
                if not link.startswith(("http://", "https://")):
                    aviso("Cole primeiro o link do vídeo no campo de cima.")
                    return
                if not assunto:
                    ent_assunto.focus()
                    return
                buscando["v"] = True
                for w in resultados.winfo_children():
                    w.destroy()

                def log(msg):
                    gravar_log(f"[{time.strftime('%d/%m %H:%M:%S')}] busca | {msg}")
                    ui_fila.put(lambda m=msg.strip(): busca_estado.set(m))

                def rodar():
                    try:
                        _, achados = encontrar_trecho(link, assunto, log)
                        ui_fila.put(lambda: mostrar_achados(achados))
                    except Exception as e:  # noqa: BLE001
                        gravar_log(f"ERRO busca: {e}")

                        def falhou(msg=str(e)):
                            buscando["v"] = False
                            busca_estado.set("Não consegui procurar: " + msg[:200])
                        ui_fila.put(falhou)
                busca_estado.set("Procurando...")
                threading.Thread(target=rodar, daemon=True).start()

            Botao(l2, "Encontrar", encontrar, primario=True, bg=COR["painel"]).pack(side="right")
            ent_assunto.bind("<Return>", lambda e: encontrar())

        def valor():
            """(ok, trecho): trecho None = video inteiro."""
            if not t["on"]:
                return True, None
            a, b = ler_tempo(ent_ini.get()), ler_tempo(ent_fim.get())
            if a is None or b is None or b <= a:
                aviso("Preencha De e Até (ex.: 1:12:40 e 1:16:05), com o fim depois do começo.\n"
                                    "Ou desligue \"Só um trecho\" para usar o vídeo inteiro.")
                return False, None
            return True, [a, b]
        t["valor"] = valor

        def fechar():
            """Depois de mandar baixar: fecha o painel e limpa tudo para o proximo video."""
            for e_ in (ent_ini, ent_fim):
                e_.delete(0, "end")
            atualizar_duracao()
            if com_busca:
                ent_assunto.delete(0, "end")
                for w in resultados.winfo_children():
                    w.destroy()
                busca_estado.set("Escreva o assunto (ex.: quando falam do Conor) e clique em Encontrar.")
            if t["on"]:
                alternar()
        t["fechar"] = fechar
        return t

    # ---- aba: baixar da internet
    linha_link = tk.Frame(aba_baixar, bg=COR["fundo"])
    linha_link.pack(fill="x", pady=(P(18), P(0)))
    campo = entrada(linha_link)
    campo.insert(0, "")
    campo.pack(side="left", fill="x", expand=True, ipady=P(10))
    Botao(linha_link, "Baixar", lambda: adicionar_link("baixar")).pack(side="left", padx=(P(10), P(0)))
    btn_legendar = Botao(linha_link, "Baixar e legendar", lambda: adicionar_link("legendar"), primario=True)
    btn_legendar.pack(side="left", padx=(P(10), P(0)))

    DICA_LINK = "Cole um link do YouTube, X ou Instagram."
    dica_link = tk.StringVar(value=DICA_LINK)

    def atualizar_opcoes(*_):
        """Só áudio: sem vídeo, sem legenda e sem preparação para o Premiere."""
        so_audio = opc_audio["on"]
        btn_legendar.ativar(not so_audio)
        opc_premiere["ativar"](not so_audio)
        try:
            combo_qual.configure(state="disabled" if so_audio else ("disabled" if qual["link"] and
                                 combo_qual.get().startswith("Buscando") else "readonly"))
        except NameError:
            pass
        if so_audio:
            dica_link.set("Só o áudio: salva um arquivo .mp3 (sem vídeo e sem legenda).")
        elif not opc_premiere["on"]:
            dica_link.set("Sem preparar para o Premiere: o vídeo fica exatamente como veio do site, sem conversão.")
        else:
            dica_link.set(DICA_LINK)
    linha_opcoes = tk.Frame(aba_baixar, bg=COR["fundo"])
    linha_opcoes.pack(fill="x", pady=(P(10), P(0)))
    opc_audio = criar_opcao(linha_opcoes, "Baixar só o áudio (MP3)", False, atualizar_opcoes)
    opc_audio["canvas"].pack(side="left")
    opc_premiere = criar_opcao(linha_opcoes, "Preparar para o Premiere depois", ler_config().get("preparar_premiere", True),
                               lambda on: (salvar_config(preparar_premiere=on), opc_at_prem["marcar"](on),
                                           atualizar_opcoes()))
    opc_premiere["canvas"].pack(side="left", padx=(P(24), P(0)))
    tk.Label(linha_opcoes, text="Qualidade", font=f_negrito, bg=COR["fundo"], fg=COR["texto"]
             ).pack(side="left", padx=(P(24), P(8)))
    combo_qual = ttk.Combobox(linha_opcoes, style="FC.TCombobox", state="readonly", width=34, font=("Segoe UI", 10))
    combo_qual.pack(side="left")
    qual = {"alturas": [0], "link": None, "geracao": 0, "cache": {}, "pendente": None}

    def qual_mostrar(rotulos, alturas, escolha=0):
        combo_qual.configure(values=rotulos, state="readonly")
        combo_qual.current(escolha)
        qual["alturas"] = alturas

    def qual_padrao(texto="Padrão (até 1080p)"):
        qual_mostrar([texto], [0])

    def qual_rotulo(o, padrao):
        h = o["altura"]
        marca = {2160: " 4K", 1440: " 2K", 1080: " Full HD", 720: " HD"}.get(h, "")
        fps = f" · {o['fps']} fps" if o["fps"] and o["fps"] > 30 else ""
        tam = (f" · ~{o['bytes'] / 1e9:.2f} GB" if o["bytes"] >= 1e9 else f" · ~{o['bytes'] / 1e6:.0f} MB") \
            if o["bytes"] else ""
        return f"{h}p{marca}{fps}{tam}" + ("  (padrão)" if padrao else "")

    def qual_aplicar(info):
        ops = info["opcoes"]
        if len(ops) < 2:
            qual_padrao("Só existe uma qualidade" if ops else "Padrão (até 1080p)")
            return
        pad = next((i for i, o in enumerate(ops) if o["altura"] <= 1080), len(ops) - 1)
        qual_mostrar([qual_rotulo(o, i == pad) for i, o in enumerate(ops)], [o["altura"] for o in ops], pad)

    def qual_buscar():
        """Roda sozinho quando o link muda: pergunta ao site as qualidades e enche a lista."""
        link = campo.get().strip().strip('"')
        if link == qual["link"]:
            return
        qual["link"] = link
        qual["geracao"] += 1
        ger = qual["geracao"]
        if not re.match(r"https?://", link) or "instagram.com" in link or Path(link).is_file():
            qual_padrao()
            return
        if link in qual["cache"]:
            qual_aplicar(qual["cache"][link])
            return
        combo_qual.configure(values=["Buscando as qualidades..."], state="disabled")
        combo_qual.current(0)
        qual["alturas"] = [0]

        def rodar():
            try:
                info = listar_qualidades(link)
            except Exception as e:  # noqa: BLE001
                info = None
                gravar_log(f"qualidades nao listadas: {str(e)[:200]}")

            def na_tela():
                if ger != qual["geracao"]:   # o link mudou enquanto buscava
                    return
                if info is None:
                    qual_padrao("Padrão (até 1080p) · não consegui listar")
                    return
                qual["cache"][link] = info
                qual_aplicar(info)
            ui_fila.put(na_tela)
        threading.Thread(target=rodar, daemon=True).start()

    def qual_agendar(*_):
        if qual["pendente"]:
            root.after_cancel(qual["pendente"])
        qual["pendente"] = root.after(600, qual_buscar)
    for ev in ("<KeyRelease>", "<<Paste>>", "<<Cut>>", "<FocusOut>"):
        campo.bind(ev, qual_agendar, add="+")
    qual_padrao()

    tk.Label(aba_baixar, textvariable=dica_link, font=f_status, bg=COR["fundo"],
             fg=COR["apagado"]).pack(anchor="w", pady=(P(6), P(0)))
    atualizar_opcoes()
    trecho_link = criar_trecho(aba_baixar, lambda: campo.get().strip().strip('"'), True)

    # ---- aba: do computador
    linha_pc = tk.Frame(aba_pc, bg=COR["fundo"])
    linha_pc.pack(fill="x", pady=(P(18), P(0)))
    campo_pc = entrada(linha_pc)
    campo_pc.pack(side="left", fill="x", expand=True, ipady=P(10))

    def escolher_arquivo():
        arq = filedialog.askopenfilename(title="Escolha um vídeo para legendar", filetypes=[
            ("Vídeos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv *.mpg *.mpeg *.ts *.mts"), ("Todos os arquivos", "*.*")])
        if arq:
            campo_pc.delete(0, "end")
            campo_pc.insert(0, str(Path(arq)))

    Botao(linha_pc, "Escolher arquivo", escolher_arquivo).pack(side="left", padx=(P(10), P(0)))
    Botao(linha_pc, "Legendar", lambda: adicionar_pc(), primario=True).pack(side="left", padx=(P(10), P(0)))
    tk.Label(aba_pc, text="Escolha um vídeo (ou cole o caminho). O original não é alterado; o .srt e o vídeo "
             "legendado ficam na mesma pasta dele.", font=f_status, bg=COR["fundo"], fg=COR["apagado"]
             ).pack(anchor="w", pady=(P(6), P(0)))
    trecho_pc = criar_trecho(aba_pc, lambda: "", False)

    # ---- opcoes comuns: idioma, estilo e pasta
    idioma = {"v": ler_config().get("idioma_legenda", "pt") if ler_config().get("idioma_legenda") in ("pt", "en") else "pt"}
    linha_idioma = tk.Frame(opcoes, bg=COR["fundo"])
    linha_idioma.pack(fill="x", pady=(P(16), P(0)))
    tk.Label(linha_idioma, text="LEGENDA EM", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left")
    seletor = Tela(linha_idioma, width=236, height=36, bg=COR["fundo"], highlightthickness=0, bd=0)
    seletor.pack(side="left", padx=P(14))

    def desenhar_seletor():
        seletor.delete("all")
        seletor.create_rectangle(1, 3, 228, 33, fill=COR["campo"], outline=COR["borda"])
        for k, (cod, rot) in enumerate((("pt", "Português"), ("en", "English"))):
            x0 = 3 + k * 112
            tag = f"lg_{cod}"
            ativo = idioma["v"] == cod
            seletor.create_rectangle(x0, 5, x0 + 111, 31, fill=COR["sel"] if ativo else COR["campo"], outline="",
                                     tags=(tag,))
            seletor.create_text(x0 + 56, 18, text=rot, font=f_negrito,
                                fill="#ffffff" if ativo else COR["fraco"], tags=(tag,))
            seletor.tag_bind(tag, "<Button-1>", lambda e, c=cod: escolher_idioma(c))
            seletor.tag_bind(tag, "<Enter>", lambda e: seletor.configure(cursor="hand2"))
            seletor.tag_bind(tag, "<Leave>", lambda e: seletor.configure(cursor=""))

    def escolher_idioma(cod):
        idioma["v"] = cod
        salvar_config(idioma_legenda=cod)
        desenhar_seletor()
        agendar_previa()
    desenhar_seletor()

    # estilo em uso (os estilos sao editados na aba Estilo da legenda)
    estilos, uso_inicial = estilos_salvos()
    em_uso = {"v": uso_inicial}   # estilo aberto no editor
    cfg_uso = ler_config()        # estilo de cada orientacao de video (um para horizontal, outro para vertical)
    uso = {k: (cfg_uso.get(f"estilo_{k}") if cfg_uso.get(f"estilo_{k}") in estilos else uso_inicial) for k in ("h", "v")}
    tk.Label(linha_idioma, text="ESTILO", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left", padx=(P(16), P(0)))
    combos_uso = {}
    for k_, rot_ in (("h", "Horizontal"), ("v", "Vertical")):
        tk.Label(linha_idioma, text=rot_, font=f_status, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left", padx=(P(10), P(0)))
        combos_uso[k_] = ttk.Combobox(linha_idioma, style="FC.TCombobox", state="readonly", width=11, font=("Segoe UI", 10))
        combos_uso[k_].pack(side="left", padx=(P(6), P(0)))
    link_editar = tk.Label(linha_idioma, text="Editar", font=f_negrito, bg=COR["fundo"], fg=COR["destaque2"],
                           cursor="hand2")
    link_editar.pack(side="left", padx=(P(14), P(0)))
    link_editar.bind("<Button-1>", lambda e: mostrar_aba("estilo"))

    linha_pasta = tk.Frame(opcoes, bg=COR["fundo"])
    linha_pasta.pack(fill="x", pady=(P(14), P(0)))
    tk.Label(linha_pasta, text="SALVAR EM", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left")
    pasta_var = tk.StringVar(value=str(SAIDA_BASE))
    tk.Label(linha_pasta, textvariable=pasta_var, font=f_texto, bg=COR["fundo"], fg=COR["texto"],
             anchor="w").pack(side="left", fill="x", expand=True, padx=P(12))

    def escolher_pasta():
        nova = filedialog.askdirectory(title="Onde salvar os vídeos?", initialdir=pasta_var.get(), mustexist=False)
        if nova:
            nova = str(Path(nova))
            pasta_var.set(nova)
            salvar_config(pasta_saida=nova)

    Botao(linha_pasta, "Escolher pasta", escolher_pasta).pack(side="right")

    # ================================================================ aba: transcrever
    tr = {"video": None, "srt": None, "idioma": None, "rodando": False, "trab": None, "feitos": []}
    linha_tr = tk.Frame(aba_trans, bg=COR["fundo"])
    linha_tr.pack(fill="x")
    campo_tr = entrada(linha_tr)
    campo_tr.pack(side="left", fill="x", expand=True, ipady=P(10))

    def abrir_na_pasta(arq):
        if sys.platform == "win32" and Path(arq).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(arq))])

    def escolher_video_tr():
        arq = filedialog.askopenfilename(title="Escolha um vídeo para transcrever", filetypes=[
            ("Vídeos e áudios", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.flv *.mpg *.mpeg *.ts *.mts *.mp3 *.wav *.m4a"),
            ("Todos os arquivos", "*.*")])
        if arq:
            campo_tr.delete(0, "end")
            campo_tr.insert(0, str(Path(arq)))

    btn_tr_escolher = Botao(linha_tr, "Escolher arquivo", escolher_video_tr)
    btn_tr_escolher.pack(side="left", padx=(P(10), P(0)))
    btn_tr_go = Botao(linha_tr, "Transcrever", lambda: transcrever_agora(), primario=True)
    btn_tr_go.pack(side="left", padx=(P(10), P(0)))
    tk.Label(aba_trans, text="Gera o .srt na língua que a pessoa fala no vídeo, ao lado dele. Depois você pode "
             "corrigir esse .srt à mão e gerar uma tradução.", font=f_status, bg=COR["fundo"], fg=COR["apagado"],
             anchor="w", justify="left").pack(anchor="w", pady=(P(6), P(0)))
    status_tr = tk.StringVar(value="")
    linha_st = tk.Frame(aba_trans, bg=COR["fundo"])
    linha_st.pack(fill="x", pady=(P(14), P(0)))
    tk.Label(linha_st, textvariable=status_tr, font=f_texto, bg=COR["fundo"], fg=COR["destaque2"], anchor="w",
             justify="left", wraplength=P(760)).pack(side="left")
    btn_tr_parar = Botao(linha_st, "Parar", lambda: tr["trab"] and tr["trab"].cancelar())

    painel_tr = tk.Frame(aba_trans, bg=COR["painel"], padx=P(16), pady=P(14), highlightthickness=1,
                         highlightbackground=COR["borda"])
    l_orig = tk.Frame(painel_tr, bg=COR["painel"])
    l_orig.pack(fill="x")
    rot_orig = tk.Label(l_orig, text="", font=f_secao, bg=COR["painel"], fg=COR["fraco"])
    rot_orig.pack(side="left")
    nome_orig = tk.Label(l_orig, text="", font=f_texto, bg=COR["painel"], fg=COR["texto"], anchor="w")
    nome_orig.pack(side="left", padx=(P(12), P(0)))
    l_trad = tk.Frame(painel_tr, bg=COR["painel"])
    l_trad.pack(fill="x", pady=(P(14), P(0)))
    tk.Label(l_trad, text="TRADUZIR PARA", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
    combo_lingua = ttk.Combobox(l_trad, style="FC.TCombobox", state="readonly", width=20, font=("Segoe UI", 10))
    combo_lingua.pack(side="left", padx=(P(12), P(12)))
    btn_tr_traduzir = Botao(l_trad, "Traduzir", lambda: traduzir_agora(), primario=True, bg=COR["painel"])
    btn_tr_traduzir.pack(side="left")
    lista_feitos = tk.Frame(painel_tr, bg=COR["painel"])
    lista_feitos.pack(fill="x", pady=(P(10), P(0)))

    def mostrar_feitos():
        for w in lista_feitos.winfo_children():
            w.destroy()
        for arq, rot in tr["feitos"]:
            l = tk.Frame(lista_feitos, bg=COR["painel"])
            l.pack(fill="x", pady=(P(4), P(0)))
            tk.Label(l, text=rot, font=f_secao, bg=COR["painel"], fg=COR["fraco"], width=14, anchor="w").pack(side="left")
            tk.Label(l, text=Path(arq).name, font=f_texto, bg=COR["painel"], fg=COR["texto"]).pack(side="left")
            ab = tk.Label(l, text="Mostrar na pasta", font=f_negrito, bg=COR["painel"], fg=COR["destaque2"], cursor="hand2")
            ab.pack(side="left", padx=(P(14), P(0)))
            ab.bind("<Button-1>", lambda e, a=arq: abrir_na_pasta(a))

    def atualizar_tr():
        ocupado = tr["rodando"]
        btn_tr_go.ativar(not ocupado)
        btn_tr_traduzir.ativar(not ocupado)
        if ocupado:
            btn_tr_parar.pack(side="left", padx=(P(14), P(0)))
        else:
            btn_tr_parar.pack_forget()

    def rodar_tr(tarefa, concluir):
        """Roda uma tarefa pesada fora da janela (cancelavel). tarefa(log) -> resultado; concluir(resultado)."""
        if tr["rodando"]:
            return
        tr["rodando"] = True
        trab = Trabalho()
        tr["trab"] = trab

        def log(msg):
            if trab.cancelado.is_set():
                raise Cancelado()
            gravar_log(f"[{time.strftime('%d/%m %H:%M:%S')}] transcrever | {msg}")
            texto = msg.strip()
            if texto:
                ui_fila.put(lambda m=texto: status_tr.set(m))

        def fio():
            CTX.trabalho = trab
            try:
                res = tarefa(log)
                ui_fila.put(lambda: concluir(res))
            except Cancelado:
                ui_fila.put(lambda: status_tr.set("Interrompido."))
            except Exception as e:  # noqa: BLE001
                if trab.cancelado.is_set():
                    ui_fila.put(lambda: status_tr.set("Interrompido."))
                else:
                    gravar_log(f"ERRO transcrever: {e}")
                    ui_fila.put(lambda m=str(e): status_tr.set("Erro: " + m[:400]))
            finally:
                tr["rodando"] = False
                tr["trab"] = None
                ui_fila.put(atualizar_tr)
        status_tr.set("Começando...")
        atualizar_tr()
        threading.Thread(target=fio, daemon=True).start()

    def preparar_traducao(idioma_orig):
        opcoes_ = [(c, n[0]) for c, n in LINGUAS.items() if c != idioma_orig]
        tr["opcoes"] = opcoes_
        combo_lingua.configure(values=[n for _, n in opcoes_])
        padrao = "pt" if idioma_orig != "pt" else "en"
        combo_lingua.current([c for c, _ in opcoes_].index(padrao))

    def transcrever_agora():
        arq = campo_tr.get().strip().strip('"')
        if not arq:
            escolher_video_tr()
            arq = campo_tr.get().strip().strip('"')
            if not arq:
                return
        if not Path(arq).is_file():
            aviso("Não encontrei esse arquivo. Use \"Escolher arquivo\".")
            return
        painel_tr.pack_forget()
        tr.update(video=Path(arq), srt=None, idioma=None, feitos=[])

        def pronto(res):
            srt, idioma = res
            tr.update(srt=srt, idioma=idioma, feitos=[(srt, "ORIGINAL")])
            nome_idioma = LINGUAS.get(idioma, (idioma.upper(),))[0]
            rot_orig.configure(text="IDIOMA DA FALA")
            nome_orig.configure(text=nome_idioma)
            preparar_traducao(idioma)
            mostrar_feitos()
            painel_tr.pack(fill="x", pady=(P(16), P(0)))
            status_tr.set("Pronto. Se quiser, corrija o .srt antes de traduzir: a tradução usa o arquivo como estiver.")
        rodar_tr(lambda log: transcrever_para_srt(tr["video"], log), pronto)

    def traduzir_agora():
        if not tr["srt"] or not Path(tr["srt"]).exists():
            aviso("Não encontrei o .srt original. Transcreva o vídeo de novo.")
            return
        escolhido = combo_lingua.current()
        if escolhido < 0:
            return
        codigo, nome = tr["opcoes"][escolhido]

        def pronto(destino):
            tr["feitos"].append((destino, "TRADUÇÃO"))
            mostrar_feitos()
            status_tr.set(f"Pronto: {Path(destino).name}")
        rodar_tr(lambda log: traduzir_srt(tr["srt"], tr["idioma"], codigo, log, tr["video"]), pronto)
    campo_tr.bind("<Return>", lambda e: transcrever_agora())
    atualizar_tr()

    # ================================================================ aba: achar trechos
    at = {"dados": None, "link": None, "rodando": False, "trab": None, "enviados": []}
    linha_at = tk.Frame(aba_trechos, bg=COR["fundo"])
    linha_at.pack(fill="x")
    campo_at = entrada(linha_at)
    campo_at.pack(side="left", fill="x", expand=True, ipady=P(10))
    btn_at_carregar = Botao(linha_at, "Carregar vídeo", lambda: carregar_video(), primario=True)
    btn_at_carregar.pack(side="left", padx=(P(10), P(0)))
    tk.Label(aba_trechos, text="Cole o link de um vídeo ou live (pode ter horas). O programa lê a fala dele uma vez só; "
             "depois você procura quantos trechos quiser e manda cada um para a fila de downloads.",
             font=f_status, bg=COR["fundo"], fg=COR["apagado"], anchor="w", justify="left",
             wraplength=P(900)).pack(anchor="w", pady=(P(6), P(0)))
    status_at = tk.StringVar(value="")
    linha_at2 = tk.Frame(aba_trechos, bg=COR["fundo"])
    linha_at2.pack(fill="x", pady=(P(12), P(0)))
    tk.Label(linha_at2, textvariable=status_at, font=f_texto, bg=COR["fundo"], fg=COR["destaque2"], anchor="w",
             justify="left", wraplength=P(780)).pack(side="left")
    btn_at_parar = Botao(linha_at2, "Parar", lambda: at["trab"] and at["trab"].cancelar())

    painel_at = tk.Frame(aba_trechos, bg=COR["painel"], padx=P(16), pady=P(14), highlightthickness=1,
                         highlightbackground=COR["borda"])
    titulo_at = tk.Label(painel_at, text="", font=f_negrito, bg=COR["painel"], fg=COR["texto"], anchor="w")
    titulo_at.pack(fill="x")
    a1 = tk.Frame(painel_at, bg=COR["painel"])
    a1.pack(fill="x", pady=(P(12), P(0)))
    tk.Label(a1, text="PROCURAR ASSUNTO", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
    ent_at_assunto = entrada(a1)
    ent_at_assunto.pack(side="left", fill="x", expand=True, padx=(P(10), P(10)), ipady=P(6))
    btn_at_buscar = Botao(a1, "Encontrar", lambda: procurar_assunto(), primario=True, bg=COR["painel"])
    btn_at_buscar.pack(side="right")
    busca_at = tk.StringVar(value="Escreva o assunto (ex.: quando falam do Conor) e clique em Encontrar.")
    tk.Label(painel_at, textvariable=busca_at, font=f_status, bg=COR["painel"], fg=COR["fraco"], anchor="w",
             justify="left", wraplength=P(860)).pack(fill="x", pady=(P(8), P(0)))
    caixa_ach = tk.Frame(painel_at, bg=COR["painel"])
    caixa_ach.pack(fill="x")
    cv_ach = tk.Canvas(caixa_ach, bg=COR["painel"], highlightthickness=0, bd=0, height=1)
    sb_ach = ttk.Scrollbar(caixa_ach, orient="vertical", command=cv_ach.yview, style="FC.Vertical.TScrollbar")
    cv_ach.configure(yscrollcommand=sb_ach.set)
    cv_ach.pack(side="left", fill="x", expand=True)
    achados_at = tk.Frame(cv_ach, bg=COR["painel"])
    janela_ach = cv_ach.create_window((0, 0), window=achados_at, anchor="nw")
    cv_ach.bind("<Configure>", lambda e: cv_ach.itemconfig(janela_ach, width=e.width))

    def ajustar_ach(*_):
        """Altura da lista de trechos: cresce ate ~6 linhas e depois rola."""
        achados_at.update_idletasks()
        h = achados_at.winfo_reqheight()
        cv_ach.configure(height=max(1, min(h, P(260))), scrollregion=(0, 0, 0, h))
        if h > P(260):
            sb_ach.pack(side="right", fill="y")
        else:
            sb_ach.pack_forget()

    def rolar_ach(e):
        if sb_ach.winfo_ismapped():
            cv_ach.yview_scroll(-1 if (e.delta > 0 or getattr(e, "num", 0) == 4) else 1, "units")
    for w_ in (cv_ach, achados_at):
        w_.bind("<Enter>", lambda e: (root.bind_all("<MouseWheel>", rolar_ach), root.bind_all("<Button-4>", rolar_ach),
                                      root.bind_all("<Button-5>", rolar_ach)))
        w_.bind("<Leave>", lambda e: (root.unbind_all("<MouseWheel>"), root.unbind_all("<Button-4>"),
                                      root.unbind_all("<Button-5>")))
    a2 = tk.Frame(painel_at, bg=COR["painel"])
    a2.pack(fill="x", pady=(P(14), P(0)))
    tk.Label(a2, text="DE", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
    ent_at_ini = entrada(a2, 9)
    ent_at_ini.pack(side="left", padx=(P(8), P(16)), ipady=P(6))
    tk.Label(a2, text="ATÉ", font=f_secao, bg=COR["painel"], fg=COR["fraco"]).pack(side="left")
    ent_at_fim = entrada(a2, 9)
    ent_at_fim.pack(side="left", padx=(P(8), P(16)), ipady=P(6))
    dur_at = tk.StringVar(value="ex.: 1:12:40")
    tk.Label(a2, textvariable=dur_at, font=f_status, bg=COR["painel"], fg=COR["apagado"]).pack(side="left")

    def atualizar_dur_at(*_):
        a, b = ler_tempo(ent_at_ini.get()), ler_tempo(ent_at_fim.get())
        dur_at.set(f"duração: {mostrar_tempo(b - a)}" if a is not None and b is not None and b > a else "ex.: 1:12:40")
    for e_ in (ent_at_ini, ent_at_fim):
        e_.bind("<KeyRelease>", atualizar_dur_at)

    def ver_no_navegador():
        if at["link"]:
            webbrowser.open(link_no_tempo(at["link"], ler_tempo(ent_at_ini.get()) or 0))
    Botao(a2, "Ver no navegador", ver_no_navegador, bg=COR["painel"]).pack(side="right")
    a3 = tk.Frame(painel_at, bg=COR["painel"])
    a3.pack(fill="x", pady=(P(14), P(0)))
    opc_at_prem = criar_opcao(a3, "Preparar para o Premiere depois", ler_config().get("preparar_premiere", True),
                              lambda on: (salvar_config(preparar_premiere=on), opc_premiere["marcar"](on),
                                          atualizar_opcoes()))
    opc_at_prem["canvas"].configure(bg=COR["painel"])
    opc_at_prem["canvas"].pack(side="left")
    Botao(a3, "Baixar e legendar o trecho", lambda: enviar_trecho("legendar"), primario=True,
          bg=COR["painel"]).pack(side="right")
    Botao(a3, "Baixar o trecho", lambda: enviar_trecho("baixar"), bg=COR["painel"]).pack(side="right", padx=(P(0), P(10)))
    rodape_at = tk.Label(painel_at, text="", font=f_status, bg=COR["painel"], fg=COR["fraco"], anchor="w",
                         justify="left", wraplength=P(860))
    rodape_at.pack(fill="x", pady=(P(10), P(0)))
    lista_env = tk.Frame(painel_at, bg=COR["painel"])
    lista_env.pack(fill="x")

    def atualizar_at():
        ocupado = at["rodando"]
        btn_at_carregar.ativar(not ocupado)
        btn_at_buscar.ativar(not ocupado)
        if ocupado:
            btn_at_parar.pack(side="left", padx=(P(14), P(0)))
        else:
            btn_at_parar.pack_forget()

    def rodar_at(tarefa, concluir):
        if at["rodando"]:
            return
        at["rodando"] = True
        trab = Trabalho()
        at["trab"] = trab

        def log(msg):
            if trab.cancelado.is_set():
                raise Cancelado()
            gravar_log(f"[{time.strftime('%d/%m %H:%M:%S')}] trechos | {msg}")
            if msg.strip():
                ui_fila.put(lambda m=msg.strip(): (busca_at if at["dados"] else status_at).set(m))

        def fio():
            CTX.trabalho = trab
            try:
                res = tarefa(log)
                ui_fila.put(lambda: concluir(res))
            except Cancelado:
                ui_fila.put(lambda: (busca_at if at["dados"] else status_at).set("Interrompido."))
            except Exception as e:  # noqa: BLE001
                gravar_log(f"ERRO trechos: {e}")
                msg = "Interrompido." if trab.cancelado.is_set() else "Não consegui: " + str(e)[:400]
                ui_fila.put(lambda m=msg: (busca_at if at["dados"] else status_at).set(m))
            finally:
                at["rodando"] = False
                at["trab"] = None
                ui_fila.put(atualizar_at)
        atualizar_at()
        threading.Thread(target=fio, daemon=True).start()

    def carregar_video():
        link = campo_at.get().strip().strip('"')
        if not link.startswith(("http://", "https://")):
            aviso("Cole o link do vídeo (começa com https://).")
            return
        painel_at.pack_forget()
        at.update(dados=None, link=link, enviados=[])
        status_at.set("Lendo o vídeo...")

        def pronto(d):
            at["dados"] = d
            tit = d.get("titulo") or link
            dur = f"  ·  {mostrar_tempo(d['duracao'])}" if d.get("duracao") else ""
            titulo_at.configure(text=tit + dur)
            status_at.set("Vídeo carregado. Procure um assunto ou preencha De/Até, e mande para a fila.")
            for w in achados_at.winfo_children():
                w.destroy()
            ajustar_ach()
            busca_at.set("Escreva o assunto (ex.: quando falam do Conor) e clique em Encontrar.")
            mostrar_enviados()
            painel_at.pack(fill="x", pady=(P(16), P(0)))
            ent_at_assunto.focus()
        rodar_at(lambda log: carregar_transcricao(link, log), pronto)

    def procurar_assunto():
        assunto = ent_at_assunto.get().strip()
        if not at["dados"] or not assunto:
            ent_at_assunto.focus()
            return
        for w in achados_at.winfo_children():
            w.destroy()
        ajustar_ach()
        busca_at.set("Procurando...")

        def pronto(achados):
            if not achados:
                busca_at.set("Não encontrei esse assunto no vídeo. Tente com outras palavras.")
                return
            busca_at.set(f"{len(achados)} trecho(s), na ordem do vídeo. Clique num para usar (os tempos vão para "
                         "De/Até) e confira com \"Ver no navegador\".")
            for a in achados:
                l = tk.Frame(achados_at, bg=COR["linha"], cursor="hand2", padx=P(10), pady=P(6))
                l.pack(fill="x", pady=(P(6), P(0)))
                rot = f"{mostrar_tempo(a['inicio'])} – {mostrar_tempo(a['fim'])}"
                t1 = tk.Label(l, text=rot, font=f_negrito, bg=COR["linha"], fg=COR["destaque2"], cursor="hand2")
                t1.pack(side="left")
                t2 = tk.Label(l, text=a["resumo"] or a["fala"], font=f_texto, bg=COR["linha"], fg=COR["texto"],
                              anchor="w", justify="left", wraplength=P(700), cursor="hand2")
                t2.pack(side="left", padx=(P(12), P(0)))
                for w in (l, t1, t2):
                    w.bind("<Button-1>", lambda e, aa=a: usar_achado_at(aa))
            ajustar_ach()
        rodar_at(lambda log: buscar_assunto(at["dados"], assunto, log), pronto)

    def usar_achado_at(a):
        for e_, v in ((ent_at_ini, a["inicio"]), (ent_at_fim, a["fim"])):
            e_.delete(0, "end")
            e_.insert(0, mostrar_tempo(v))
        atualizar_dur_at()

    def mostrar_enviados():
        for w in lista_env.winfo_children():
            w.destroy()
        rodape_at.configure(text=f"Salvando em: {pasta_var.get()}" + (
            "\nEnviados para a fila neste vídeo (acompanhe na aba Baixar da internet):" if at["enviados"] else ""))
        for rot in at["enviados"]:
            tk.Label(lista_env, text="✓  " + rot, font=f_texto, bg=COR["painel"], fg=COR["texto"],
                     anchor="w").pack(fill="x", pady=(P(2), P(0)))

    def enviar_trecho(modo):
        a, b = ler_tempo(ent_at_ini.get()), ler_tempo(ent_at_fim.get())
        if not at["link"] or a is None or b is None or b <= a:
            aviso("Preencha De e Até (ex.: 1:12:40 e 1:16:05), com o fim depois do começo.\n"
                  "Ou clique num trecho achado pela busca.")
            return
        adicionar(at["link"], modo, [a, b], premiere=opc_at_prem["on"])
        at["enviados"].append(f"{mostrar_tempo(a)} – {mostrar_tempo(b)}"
                              + ("  (com legenda)" if modo != "baixar" else ""))
        mostrar_enviados()
        for e_ in (ent_at_ini, ent_at_fim):
            e_.delete(0, "end")
        atualizar_dur_at()
        busca_at.set("Trecho na fila. Procure o próximo.")
        tela.yview_moveto(0)
    campo_at.bind("<Return>", lambda e: carregar_video())
    ent_at_assunto.bind("<Return>", lambda e: procurar_assunto())
    atualizar_at()

    # ================================================================ aba: estilo da legenda
    ed = {}   # variaveis do editor
    sujo = {"v": False, "carregando": False}
    esq = tk.Frame(aba_estilo, bg=COR["fundo"])
    esq.pack(side="left", fill="y")
    dir_ = tk.Frame(aba_estilo, bg=COR["fundo"])
    dir_.pack(side="left", fill="both", expand=True, padx=(P(24), P(0)))

    # escolha / salvar estilos
    l_est = tk.Frame(esq, bg=COR["fundo"])
    l_est.pack(fill="x")
    tk.Label(l_est, text="ESTILO", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left")
    combo_ed = ttk.Combobox(l_est, style="FC.TCombobox", state="readonly", width=20, font=("Segoe UI", 10))
    combo_ed.pack(side="left", padx=(P(10), P(0)), fill="x", expand=True)
    l_bot = tk.Frame(esq, bg=COR["fundo"])
    l_bot.pack(fill="x", pady=(P(10), P(0)))
    l_uso = tk.Frame(esq, bg=COR["fundo"])
    l_uso.pack(fill="x", pady=(P(8), P(0)))
    usa_var = {"h": tk.BooleanVar(), "v": tk.BooleanVar()}
    for k_, rot_ in (("h", "Usar nos vídeos horizontais"), ("v", "Usar nos vídeos verticais")):
        tk.Checkbutton(l_uso, text=rot_, variable=usa_var[k_], command=lambda kk=k_: marcar_uso(kk), font=f_texto,
                       bg=COR["fundo"], fg=COR["texto"], selectcolor=COR["campo"], activebackground=COR["fundo"],
                       activeforeground=COR["texto"], highlightthickness=0, bd=0, cursor="hand2"
                       ).pack(side="left", padx=(P(0), P(16)))
    aviso_var = tk.StringVar()
    tk.Label(esq, textvariable=aviso_var, font=f_status, bg=COR["fundo"], fg=COR["fraco"], anchor="w",
             justify="left", wraplength=P(360)).pack(fill="x", pady=(P(6), P(4)))

    def secao(texto):
        tk.Label(esq, text=texto, font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(anchor="w", pady=(P(12), P(4)))

    def linha_ed():
        f = tk.Frame(esq, bg=COR["fundo"])
        f.pack(fill="x", pady=P(2))
        return f

    def marcar(*_):
        if sujo["carregando"]:
            return
        sujo["v"] = True
        atualizar_aviso()
        agendar_previa()

    def checar(pai, texto, chave):
        ed[chave] = tk.BooleanVar()
        ed[chave].trace_add("write", marcar)
        tk.Checkbutton(pai, text=texto, variable=ed[chave], font=f_texto, bg=COR["fundo"], fg=COR["texto"],
                       selectcolor=COR["campo"], activebackground=COR["fundo"], activeforeground=COR["texto"],
                       highlightthickness=0, bd=0, cursor="hand2").pack(side="left")

    def cor_botao(pai, chave):
        ed[chave] = tk.StringVar()
        c = Tela(pai, width=38, height=24, bg=COR["fundo"], highlightthickness=0, bd=0, cursor="hand2")
        c.pack(side="left", padx=(P(8), P(0)))

        def pintar_cor(*_):
            c.delete("all")
            retangulo(c, 1, 1, 37, 23, 7, fill=ed[chave].get() or "#ffffff", outline=COR["borda"])
        ed[chave].trace_add("write", lambda *a: (pintar_cor(), marcar()))

        def escolher(e=None):
            from tkinter import colorchooser
            nova = colorchooser.askcolor(color=ed[chave].get(), parent=root, title="Escolha a cor")[1]
            if nova:
                ed[chave].set(nova.upper())
        c.bind("<Button-1>", escolher)
        return c

    def deslizar(pai, chave, de, ate, sufixo="%", largura=170):
        ed[chave] = tk.IntVar()
        rot = tk.Label(pai, font=f_status, bg=COR["fundo"], fg=COR["fraco"], width=5, anchor="e")
        sc = ttk.Scale(pai, from_=de, to=ate, orient="horizontal", variable=ed[chave], length=largura,
                       style="FC.Horizontal.TScale")
        sc.pack(side="right")
        rot.pack(side="right")
        ed[chave].trace_add("write", lambda *a: (rot.configure(text=f"{ed[chave].get()}{sufixo}"), marcar()))
        sc.configure(command=lambda v, k=chave: ed[k].set(int(round(float(v)))))

    secao("TEXTO")
    f = linha_ed()
    tk.Label(f, text="Fonte", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    ed["fonte"] = tk.StringVar()
    lista_fontes = sorted({x for x in tkfont.families(root) if not x.startswith("@")} | set(familias_da_pasta_fontes()),
                          key=str.lower)
    combo_fonte = ttk.Combobox(f, style="FC.TCombobox", textvariable=ed["fonte"], values=lista_fontes, width=24,
                               font=("Segoe UI", 10))
    combo_fonte.pack(side="left", fill="x", expand=True)
    combo_fonte.bind("<<ComboboxSelected>>", marcar)
    combo_fonte.bind("<Return>", marcar)
    combo_fonte.bind("<FocusOut>", marcar)
    f = linha_ed()
    tk.Label(f, text="Tamanho", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    deslizar(f, "tamanho", 50, 250)
    f = linha_ed()
    tk.Label(f, text="Cor", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    cor_botao(f, "cor")
    tk.Frame(f, bg=COR["fundo"], width=18).pack(side="left")
    checar(f, "Negrito", "negrito")
    f = linha_ed()
    tk.Label(f, text="Altura", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    deslizar(f, "altura", 0, 300)

    secao("CONTORNO")
    f = linha_ed()
    checar(f, "Usar contorno", "contorno")
    cor_botao(f, "cor_contorno")
    deslizar(f, "espessura", 2, 25, largura=120)

    secao("SOMBRA")
    f = linha_ed()
    checar(f, "Usar sombra", "sombra")
    cor_botao(f, "cor_sombra")
    f = linha_ed()
    tk.Label(f, text="Distância", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    deslizar(f, "dist_sombra", 0, 25)
    f = linha_ed()
    tk.Label(f, text="Opacidade", font=f_texto, bg=COR["fundo"], fg=COR["texto"], width=10, anchor="w").pack(side="left")
    deslizar(f, "opac_sombra", 0, 100)

    secao("TARJA ATRÁS DO TEXTO (substitui o contorno)")
    f = linha_ed()
    checar(f, "Usar tarja", "tarja")
    cor_botao(f, "cor_tarja")
    deslizar(f, "opac_tarja", 0, 100, largura=120)

    def abrir_fontes():
        PASTA_FONTES.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(PASTA_FONTES)
    f = linha_ed()
    f.pack_configure(pady=(P(16), P(0)))
    tk.Label(esq, text="Fonte sua que não está na lista? Coloque o .ttf/.otf na pasta de fontes e reabra o programa.",
             font=f_status, bg=COR["fundo"], fg=COR["apagado"], wraplength=P(360), justify="left", anchor="w"
             ).pack(fill="x", pady=(P(6), P(0)))
    Botao(f, "Pasta de fontes", abrir_fontes).pack(side="left")
    Botao(f, "Valores de fábrica", lambda: carregar_no_editor(ESTILO_PADRAO, marcar_sujo=True)).pack(side="left",
                                                                                                      padx=(P(10), P(0)))

    def ler_editor():
        e = {}
        for k, v in ESTILO_PADRAO.items():
            try:
                e[k] = ed[k].get()
            except (tk.TclError, KeyError):
                e[k] = v
        e["fonte"] = (e["fonte"] or "Arial").strip()
        return estilo_completo(e)

    def carregar_no_editor(est, marcar_sujo=False):
        sujo["carregando"] = True
        for k, v in estilo_completo(est).items():
            ed[k].set(v)
        sujo["carregando"] = False
        sujo["v"] = marcar_sujo
        atualizar_aviso()
        agendar_previa()

    def atualizar_aviso():
        nome = combo_ed.get()
        usos = [rot for k, rot in (("h", "horizontais"), ("v", "verticais")) if uso[k] == nome]
        if sujo["v"]:
            aviso_var.set(f"Alterações não salvas em \"{nome}\". Os vídeos só usam o estilo depois de salvar.")
        elif usos:
            aviso_var.set(f"\"{nome}\" é usado nos próximos vídeos {' e '.join(usos)}.")
        else:
            aviso_var.set(f"\"{nome}\" não está em uso. Marque acima para usar nos próximos vídeos.")
        for k in ("h", "v"):
            usa_var[k].set(uso[k] == nome)

    def atualizar_combos():
        nomes = list(estilos)
        combo_ed.configure(values=nomes)
        combo_ed.set(em_uso["v"])
        for k, cb in combos_uso.items():
            cb.configure(values=nomes)
            cb.set(uso[k])

    def usar_estilo(nome, carregar=True):   # abre o estilo no editor
        em_uso["v"] = nome
        salvar_config(estilo_em_uso=nome)
        atualizar_combos()
        if carregar:
            carregar_no_editor(estilos[nome])

    def pode_trocar(combo):
        """Antes de trocar de estilo com alteracoes nao salvas: Sim = salva e troca, Nao = descarta e troca,
        Cancelar = fica no estilo atual."""
        if not sujo["v"]:
            return True
        resp = messagebox.askyesnocancel(NOME_APP, f"Salvar as alterações feitas em \"{em_uso['v']}\" antes de trocar?\n\n"
                                         "Sim: salva e troca.\nNão: descarta as alterações e troca.\n"
                                         "Cancelar: continua neste estilo.")
        if resp is None:
            combo.set(em_uso["v"])
            return False
        if resp:
            salvar_estilo()
        else:
            sujo["v"] = False
        return True

    def trocar_no_editor(e=None):
        nome = combo_ed.get()
        if nome != em_uso["v"] and pode_trocar(combo_ed):
            usar_estilo(nome)

    def gravar_estilos():
        salvar_config(estilos=estilos, estilo_em_uso=em_uso["v"], estilo_h=uso["h"], estilo_v=uso["v"])

    def trocar_uso(k):   # combo da tela principal: qual estilo vale para essa orientacao
        uso[k] = combos_uso[k].get()
        gravar_estilos()
        atualizar_aviso()

    def marcar_uso(k):   # caixinha do editor: usar o estilo aberto nessa orientacao
        nome = em_uso["v"]
        if usa_var[k].get():
            uso[k] = nome
            gravar_estilos()
            atualizar_combos()
        elif uso[k] == nome:
            usa_var[k].set(True)
            aviso("Todo vídeo precisa de um estilo. Para trocar, escolha outro estilo na "
                                "lista ESTILO da tela principal ou marque a caixinha em outro estilo.")
        atualizar_aviso()
    combo_ed.bind("<<ComboboxSelected>>", trocar_no_editor)
    for k_, cb_ in combos_uso.items():
        cb_.bind("<<ComboboxSelected>>", lambda e, kk=k_: trocar_uso(kk))

    def salvar_estilo():
        estilos[em_uso["v"]] = ler_editor()
        gravar_estilos()
        sujo["v"] = False
        atualizar_aviso()

    def salvar_como():
        from tkinter import simpledialog
        nome = simpledialog.askstring(NOME_APP, "Nome do novo estilo (ex.: Shorts, Canal):", parent=root)
        nome = (nome or "").strip()
        if not nome:
            return
        if nome in estilos and not messagebox.askyesno(NOME_APP, f"Já existe um estilo \"{nome}\". Substituir?"):
            return
        estilos[nome] = ler_editor()
        em_uso["v"] = nome   # abre o novo no editor; para usar nos videos, marque horizontal/vertical
        gravar_estilos()
        atualizar_combos()
        sujo["v"] = False
        atualizar_aviso()

    def excluir_estilo():
        if len(estilos) <= 1:
            aviso("Esse é o único estilo; não dá para excluir.")
            return
        nome = em_uso["v"]
        if not messagebox.askyesno(NOME_APP, f"Excluir o estilo \"{nome}\"?"):
            return
        del estilos[nome]
        em_uso["v"] = next(iter(estilos))
        for k in ("h", "v"):
            if uso[k] == nome:
                uso[k] = em_uso["v"]
        gravar_estilos()
        atualizar_combos()
        carregar_no_editor(estilos[em_uso["v"]])

    Botao(l_bot, "Salvar", salvar_estilo, primario=True).pack(side="left")
    Botao(l_bot, "Salvar como novo", salvar_como).pack(side="left", padx=(P(8), P(0)))
    def renomear_estilo():
        from tkinter import simpledialog
        antigo = em_uso["v"]
        novo = simpledialog.askstring(NOME_APP, f"Novo nome para o estilo \"{antigo}\":", initialvalue=antigo, parent=root)
        novo = (novo or "").strip()
        if not novo or novo == antigo:
            return
        if novo in estilos:
            messagebox.showerror(NOME_APP, f"Já existe um estilo chamado \"{novo}\". Escolha outro nome.")
            return
        for k in list(estilos):   # mantem a ordem da lista
            valor = estilos.pop(k)
            estilos[novo if k == antigo else k] = valor
        for k in ("h", "v"):
            if uso[k] == antigo:
                uso[k] = novo
        em_uso["v"] = novo
        gravar_estilos()
        atualizar_combos()
        atualizar_aviso()
    Botao(l_bot, "Renomear", renomear_estilo).pack(side="left", padx=(P(8), P(0)))
    Botao(l_bot, "Excluir", excluir_estilo).pack(side="left", padx=(P(8), P(0)))

    # ---- previa
    l_prev = tk.Frame(dir_, bg=COR["fundo"])
    l_prev.pack(fill="x")
    tk.Label(l_prev, text="PRÉVIA", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left")
    formato = {"v": None}   # "h" ou "v"; None = o do ultimo video
    seletor_fmt = Tela(l_prev, width=224, height=34, bg=COR["fundo"], highlightthickness=0, bd=0)
    seletor_fmt.pack(side="right")

    def desenhar_fmt():
        c = seletor_fmt
        c.delete("all")
        c.create_rectangle(1, 2, 220, 32, fill=COR["campo"], outline=COR["borda"])
        for k, (cod, rot) in enumerate((("h", "Horizontal"), ("v", "Vertical"))):
            x0 = 3 + k * 108
            tag = f"fmt_{cod}"
            ativo = (formato["v"] or "h") == cod
            c.create_rectangle(x0, 4, x0 + 107, 30, fill=COR["sel"] if ativo else COR["campo"], outline="", tags=(tag,))
            c.create_text(x0 + 54, 17, text=rot, font=f_negrito, fill="#ffffff" if ativo else COR["fraco"], tags=(tag,))
            c.tag_bind(tag, "<Button-1>", lambda e, cd=cod: (formato.update(v=cd), escolha_prev.update(v=None),
                                                             combo_prev.set(AUTO), desenhar_fmt(), agendar_previa(0)))
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))
    desenhar_fmt()

    l_vid = tk.Frame(dir_, bg=COR["fundo"])
    l_vid.pack(fill="x", pady=(P(10), P(0)))
    tk.Label(l_vid, text="Vídeo da prévia", font=f_texto, bg=COR["fundo"], fg=COR["texto"]).pack(side="left")
    AUTO = "Automático (último vídeo)"
    escolha_prev = {"v": None, "mapa": {}}   # caminho escolhido (None = automatico)
    combo_prev = ttk.Combobox(l_vid, style="FC.TCombobox", state="readonly", font=("Segoe UI", 10))
    combo_prev.pack(side="left", fill="x", expand=True, padx=(P(12), P(0)))

    def listar_videos_previa():
        mapa = {}
        for cam in candidatos_previa():
            nome = Path(cam).name
            if nome in mapa:
                nome = f"{nome}  ({Path(cam).parent.name})"
            mapa[nome] = cam
        escolha_prev["mapa"] = mapa
        combo_prev.configure(values=[AUTO] + list(mapa))
        atual = next((n for n, c in mapa.items() if c == escolha_prev["v"]), None)
        if atual is None:
            escolha_prev["v"] = None
        combo_prev.set(atual or AUTO)
    combo_prev.configure(postcommand=listar_videos_previa)

    def escolher_video_prev(e=None):
        nome = combo_prev.get()
        escolha_prev["v"] = escolha_prev["mapa"].get(nome)
        if escolha_prev["v"] is None:
            formato["v"] = None
        agendar_previa(0)
    combo_prev.bind("<<ComboboxSelected>>", escolher_video_prev)

    quadro = tk.Label(dir_, bg=COR["campo"], text="Gerando a prévia...", fg=COR["fraco"], font=f_texto)
    quadro.pack(fill="both", expand=True, pady=(P(10), P(0)))
    origem_var = tk.StringVar()
    tk.Label(dir_, textvariable=origem_var, font=f_status, bg=COR["fundo"], fg=COR["fraco"], anchor="w",
             justify="left", wraplength=P(420)).pack(fill="x", pady=(P(8), P(0)))
    l_mom = tk.Frame(dir_, bg=COR["fundo"])
    l_mom.pack(fill="x", pady=(P(6), P(0)))
    tk.Label(l_mom, text="Momento do vídeo", font=f_texto, bg=COR["fundo"], fg=COR["texto"]).pack(side="left")
    momento = tk.IntVar(value=30)
    ttk.Scale(l_mom, from_=0, to=100, orient="horizontal", variable=momento, style="FC.Horizontal.TScale"
              ).pack(side="left", fill="x", expand=True, padx=(P(12), P(0)))
    momento.trace_add("write", lambda *a: agendar_previa())
    previa = {"gen": 0, "after": None, "img": None, "info": {}}

    def medir(caminho):
        if caminho not in previa["info"]:
            try:
                previa["info"][caminho] = duracao_e_altura(caminho)
            except Exception:  # noqa: BLE001
                previa["info"][caminho] = None
        return previa["info"][caminho]

    def candidatos_previa():
        """Videos ja processados (do mais novo para o mais antigo), sem legenda gravada."""
        out = []
        for r in registros:
            if r.get("status") != "pronto":
                continue
            arqs = [a for a in r.get("arquivos") or [] if a.lower().endswith(".mp4") and "_legendado" not in a]
            if not arqs and Path(str(r.get("link", ""))).is_file():
                arqs = [r["link"]]
            out += [a for a in arqs if Path(a).exists()]
        return out

    def agendar_previa(atraso=450):
        if aba["v"] != "estilo":
            return
        if previa["after"]:
            root.after_cancel(previa["after"])
        previa["after"] = root.after(atraso, gerar_previa)

    def gerar_previa():
        previa["after"] = None
        previa["gen"] += 1
        gen, est, fmt = previa["gen"], ler_editor(), formato["v"]
        root.update_idletasks()
        mw, mh = max(200, quadro.winfo_width() - 8), max(200, quadro.winfo_height() - 8)
        mom, lang = momento.get(), idioma["v"]

        def rodar():
            try:
                escolhido = None
                fixo = escolha_prev["v"]
                if fixo and Path(fixo).exists() and medir(fixo):   # video escolhido na lista
                    info = medir(fixo)
                    formato["v"] = "v" if info[2] > info[1] else "h"
                    ui_fila.put(desenhar_fmt)
                    escolhido = (fixo, info)
                for cand in ([] if escolhido else candidatos_previa()[:12]):
                    info = medir(cand)
                    if not info:
                        continue
                    vertical = info[2] > info[1]
                    if fmt is None:
                        formato["v"] = fmt_ = "v" if vertical else "h"
                        ui_fila.put(desenhar_fmt)
                    else:
                        fmt_ = fmt
                    if (fmt_ == "v") == vertical:
                        escolhido = (cand, info)
                        break
                if escolhido:
                    cand, (dur, larg, alt) = escolhido
                    texto = f"Quadro de: {Path(cand).name}"
                    video, t = cand, dur * mom / 100
                else:
                    larg, alt = (1080, 1920) if (fmt or formato["v"]) == "v" else (1920, 1080)
                    texto = "Ainda não há vídeo nesse formato: prévia sobre um fundo neutro."
                    video, t = None, 0
                saida = Path(tempfile.gettempdir()) / f"fc_previa_{os.getpid()}_{gen % 2}.png"
                renderizar_previa(video, t, larg, alt, est, saida, mw, mh, lang)

                def mostrar():
                    if gen != previa["gen"]:
                        return
                    previa["img"] = tk.PhotoImage(file=str(saida))
                    quadro.configure(image=previa["img"], text="")
                    origem_var.set(texto)
                ui_fila.put(mostrar)
            except Exception as e:  # noqa: BLE001
                gravar_log(f"previa: {e}")
                ui_fila.put(lambda m=str(e): (quadro.configure(image="", text="Não consegui gerar a prévia."),
                                              origem_var.set(m[:200])))
        threading.Thread(target=rodar, daemon=True).start()
    quadro.bind("<Configure>", lambda e: agendar_previa())

    atualizar_combos()
    carregar_no_editor(estilos[em_uso["v"]])

    # ---- lista de videos (com rolagem)
    l_videos = tk.Frame(corpo, bg=COR["fundo"])
    l_videos.pack(fill="x", padx=P(26), pady=(P(14), P(6)))
    tk.Label(l_videos, text="VÍDEOS", font=f_secao, bg=COR["fundo"], fg=COR["fraco"]).pack(side="left")
    link_avisos = tk.Label(l_videos, text="Mostrar os avisos de novo", font=f_status, bg=COR["fundo"],
                           fg=COR["destaque2"], cursor="hand2")

    def restaurar_avisos(e=None):
        salvar_config(avisos_ocultos=[])
        atualizar_link_avisos()

    def atualizar_link_avisos():
        if _ocultos():
            link_avisos.pack(side="right", padx=(P(0), P(8)))
        else:
            link_avisos.pack_forget()
    link_avisos.bind("<Button-1>", restaurar_avisos)
    atualizar_link_avisos()
    barra = Tela(corpo, height=44, bg=COR["fundo"], highlightthickness=0, bd=0)
    barra.pack(fill="x", padx=(P(24), P(14)), pady=(P(0), P(6)))
    moldura = tk.Frame(corpo, bg=COR["fundo"])
    moldura.pack(fill="both", expand=True, padx=(P(24), P(14)), pady=(P(0), P(16)))
    estilo = ttk.Style(root)
    try:
        estilo.theme_use("clam")
    except tk.TclError:
        pass
    estilo.layout("FC.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"children": [
        ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})], "sticky": "ns"})])
    estilo.configure("FC.Vertical.TScrollbar", background=COR["borda"], troughcolor=COR["fundo"], bordercolor=COR["fundo"],
                     lightcolor=COR["borda"], darkcolor=COR["borda"], gripcount=0, width=8, relief="flat")
    estilo.map("FC.Vertical.TScrollbar", background=[("active", COR["fraco"]), ("pressed", COR["destaque2"])])
    tela = tk.Canvas(moldura, bg=COR["fundo"], highlightthickness=0, bd=0)
    rolagem = ttk.Scrollbar(moldura, orient="vertical", command=tela.yview, style="FC.Vertical.TScrollbar")
    lista = tk.Frame(tela, bg=COR["fundo"])
    lista.bind("<Configure>", lambda e: tela.configure(scrollregion=tela.bbox("all")))
    janela_lista = tela.create_window((0, 0), window=lista, anchor="nw")
    tela.bind("<Configure>", lambda e: tela.itemconfigure(janela_lista, width=e.width))
    tela.configure(yscrollcommand=rolagem.set)
    tela.pack(side="left", fill="both", expand=True)
    rolagem.pack(side="right", fill="y", padx=(P(8), P(0)))
    tela.bind_all("<MouseWheel>", lambda e: tela.yview_scroll(int(-e.delta / 120), "units"))
    vazio = tk.Label(lista, text="Nenhum vídeo ainda.\nCole um link acima e escolha Baixar ou Baixar e legendar.",
                     font=f_texto, bg=COR["fundo"], fg=COR["fraco"], pady=P(60), justify="center")

    registros = ler_config().get("videos", [])   # [{id, titulo, pasta, base, status, erro}]
    for r in registros:
        r.pop("nota", None)
        r.pop("substituir", None)
    linhas = {}
    animacao = {}   # id -> posicao da barra de progresso
    marcados = set()   # ids marcados nas caixinhas

    def salvar_lista():
        salvar_config(videos=[r for r in registros if r["status"] in ("pronto", "erro", "interrompido")])

    def atualizar_vazio():
        if registros:
            vazio.pack_forget()
        else:
            vazio.pack(fill="x")

    def trabalhando(r):
        return r["status"] not in ("pronto", "erro", "fila", "interrompido") or bool(r.get("nota"))

    def cor_status(r):
        if r["status"] == "pronto" and not r.get("nota"):
            return COR["ok"]
        if r["status"] == "interrompido":
            return COR["fraco"]
        return COR["erro"] if r["status"] == "erro" else COR["destaque2"]

    def texto_status(r):
        if r["status"] == "pronto":
            return f"Pronto  ·  {r['nota']}" if r.get("nota") else "Pronto"
        return {"erro": "Erro (clique para ver)", "fila": "Na fila", "interrompido": "Interrompido"}.get(
            r["status"], r["status"])

    def etiquetas(r):
        """Pequenas etiquetas da linha: completo/trecho e legendado/sem legenda."""
        tags = ["TRECHO" if r.get("trecho") else "COMPLETO"]
        if r.get("audio"):
            tags.append("ÁUDIO MP3")
        elif r.get("modo") == "baixar":
            tags.append("SEM LEGENDA")
        else:
            tags.append("LEGENDADO EN" if r.get("lang") == "en" else "LEGENDADO")
        if not r.get("audio") and r.get("premiere") is False:
            tags.append("COM CÓPIA PREMIERE" if r.get("copia") else "SEM CONVERSÃO")
        return tags

    def pct_do(r):
        m = re.search(r"(\d+)%", r["status"])
        return int(m.group(1)) if (m and trabalhando(r)) else None

    def abrir(r):
        existentes = [a for a in r.get("arquivos") or [] if Path(a).exists()]
        if existentes and sys.platform == "win32":   # downloads novos: arquivos soltos na pasta escolhida
            principal = next((a for a in existentes if a.endswith("_legendado.mp4")), existentes[0])
            subprocess.Popen(["explorer", "/select,", principal])
            return
        if r.get("arquivos") and not existentes:
            aviso("Os arquivos desse vídeo não existem mais.\nUse o botão de baixar de novo.")
            return
        alvo = r.get("pasta") or r.get("base")
        if alvo and Path(alvo).exists() and sys.platform == "win32":
            arq = Path(alvo) / "legendado.mp4"
            if not arq.exists():
                arq = Path(alvo) / "original.mp4"
            if arq.exists():
                subprocess.Popen(["explorer", "/select,", str(arq)])
            else:
                os.startfile(alvo)
        else:
            aviso("A pasta desse vídeo não existe mais.")

    def abrir_link(r):
        webbrowser.open(link_no_tempo(r["link"], r["trecho"][0]) if r.get("trecho") else r["link"])

    def original_sem_conversao(r):
        """O video original baixado SEM conversao (e que ainda nao ganhou uma copia), ou None."""
        if r.get("audio") or r.get("premiere") is not False:
            return None
        if r.get("copia") and Path(r["copia"]).exists():
            return None
        for a in r.get("arquivos") or []:
            p = Path(a)
            if (p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and "_legendado" not in p.stem
                    and "_convertido" not in p.stem and p.exists()):
                return p
        return None

    def converter(r):
        """Cria uma copia do original pronta para o Premiere (o original fica como esta)."""
        if r["status"] != "pronto" or r.get("nota"):
            return
        origem = original_sem_conversao(r)
        if origem is None:
            return
        gb = origem.stat().st_size / 1e9
        if not confirmar(f"Converter {origem.name} para o Premiere?\n\n"
                         f"Isso cria uma cópia ao lado do original (o original não muda) e ocupa mais "
                         f"{gb:.1f} GB ou mais no disco. Em vídeos longos pode levar bastante tempo, "
                         "mas você pode continuar baixando outros vídeos enquanto isso."):
            return
        r.update(acao="converter", status="fila", erro=None)
        desenhar(r)
        enfileirar(r)

    def restilizar(r):
        """Regrava o video legendado com o estilo em uso (sem baixar nem transcrever de novo)."""
        if r["status"] not in ("pronto", "erro") or r.get("nota"):
            return
        if not arquivos_para_refazer(r.get("arquivos"), r.get("link"), r.get("trecho")):
            aviso("Não encontrei o vídeo original e o .srt desse vídeo na pasta.\n"
                                "Use o botão de baixar de novo.")
            return
        if sujo["v"] and em_uso["v"] in (uso["h"], uso["v"]) and not messagebox.askyesno(
                NOME_APP, f"O estilo \"{em_uso['v']}\" tem alterações não salvas, que não serão usadas. "
                          "Continuar com a versão salva?"):
            return
        nomes = uso["h"] if uso["h"] == uso["v"] else f"{uso['h']} (horizontal) / {uso['v']} (vertical)"
        if not confirmar(f"Refazer a legenda com o estilo \"{nomes}\"?\n\n"
                                   "O vídeo legendado atual será substituído. O original e o .srt não mudam "
                                   "(se você corrigiu o .srt, as correções entram)."):
            return
        r.update(estilo_antigo=r.get("estilo"), estilo_v_antigo=r.get("estilo_v"), nome_antigo=r.get("nome_estilo"),
                 estilo=dict(estilos[uso["h"]]), estilo_v=dict(estilos[uso["v"]]), nome_estilo=uso["h"],
                 acao="restilizar", status="fila", erro=None)
        desenhar(r)
        enfileirar(r)

    def refazer(r):
        """Baixa o mesmo link de novo (por exemplo, se o arquivo foi apagado sem querer)."""
        if not r.get("link") or r["status"] not in ("pronto", "erro", "interrompido") or r.get("nota"):
            return
        existentes = [a for a in r.get("arquivos") or [] if Path(a).exists()]
        if existentes:
            nomes = "\n".join(Path(a).name for a in existentes)
            if not confirmar("Esses arquivos ainda estão na pasta:\n\n" + nomes +
                                       "\n\nBaixar de novo mesmo assim? Os atuais só são apagados quando o novo ficar pronto."):
                return
            r["substituir"] = existentes
        r.update(status="fila", erro=None, acao=None)
        r.pop("copia", None)
        desenhar(r)
        enfileirar(r)

    def parar(r):
        """Interrompe o video (na fila ou em andamento). Fica na lista como 'Interrompido'."""
        if r["status"] in ("pronto", "erro", "interrompido") and not r.get("nota"):
            return
        r["exec"] = r.get("exec", 0) + 1   # a vaga que ele ocupa na fila deixa de valer
        trab = ativos.get(r["id"])
        if trab is not None:
            trab.cancelar()   # a thread dele encerra o ffmpeg/yt-dlp e avisa quando terminar
            r["status_antes"] = r["status"]
        else:
            r.update(status="interrompido", nota="")
            desenhar(r)
            salvar_lista()

    def reiniciar(r):
        """Recomeca o download/processamento do zero (interrompe o atual, se estiver rodando)."""
        if not r.get("link"):
            return
        if r["id"] in ativos:
            reinicio.add(r["id"])
            parar(r)
            return
        if r["status"] == "fila" or r["status"] in ("erro", "interrompido"):
            r["exec"] = r.get("exec", 0) + 1
            r.update(status="fila", erro=None, nota="")
            desenhar(r)
            enfileirar(r)

    def tirar_arquivos(r):
        """Apaga os arquivos de um video. Devolve a mensagem de erro, ou None se deu certo."""
        existentes = [a for a in r.get("arquivos") or [] if Path(a).exists()]
        try:
            if existentes:
                apagar_arquivos(existentes, r["base"])
            elif r.get("pasta") and Path(r["pasta"]).exists():   # registros antigos (uma pasta por video)
                apagar_video(r["pasta"], r["base"])
        except Exception as e:  # noqa: BLE001
            return str(e)
        return None

    def esquecer(r):
        if r in registros:
            registros.remove(r)
        marcados.discard(r["id"])
        c = linhas.pop(r["id"], None)
        if c is not None:
            c.destroy()
        animacao.pop(r["id"], None)

    def apagar(r):
        if r["status"] not in ("pronto", "erro", "interrompido") or r.get("nota"):
            aviso("Espere esse vídeo terminar para apagar.")
            return
        existentes = [a for a in r.get("arquivos") or [] if Path(a).exists()]
        if existentes:
            nomes = "\n".join(Path(a).name for a in existentes)
            if not messagebox.askyesno(NOME_APP, f"Apagar da lista e apagar estes arquivos?\n\n{nomes}"):
                return
        elif r.get("pasta") and Path(r["pasta"]).exists():
            if not messagebox.askyesno(NOME_APP, f"Apagar \"{r['titulo']}\" da lista e os arquivos dele?\n\n{r['pasta']}"):
                return
        erro = tirar_arquivos(r)
        if erro:
            messagebox.showerror(NOME_APP, f"Não consegui apagar os arquivos: {erro}")
            return
        esquecer(r)
        salvar_lista()
        atualizar_vazio()
        desenhar_barra()

    def apagar_varios():
        sel = [r for r in registros if r["id"] in marcados]
        ocupados = [r for r in sel if r["status"] not in ("pronto", "erro", "interrompido") or r.get("nota")]
        sel = [r for r in sel if r not in ocupados]
        if not sel:
            aviso("Os vídeos marcados ainda estão sendo processados. Espere terminarem.")
            return
        arqs = [Path(a).name for r in sel for a in r.get("arquivos") or [] if Path(a).exists()]
        texto = f"Apagar {len(sel)} vídeo(s) da lista e {len(arqs)} arquivo(s) da pasta?\n\n" + "\n".join(arqs[:8])
        if len(arqs) > 8:
            texto += f"\n... e mais {len(arqs) - 8}"
        if ocupados:
            texto += f"\n\n({len(ocupados)} ainda em andamento ficam de fora.)"
        if not messagebox.askyesno(NOME_APP, texto):
            return
        falhas = []
        for r in sel:
            erro = tirar_arquivos(r)
            if erro:
                falhas.append(f"{r['titulo']}: {erro}")
            else:
                esquecer(r)
        salvar_lista()
        atualizar_vazio()
        desenhar_barra()
        if falhas:
            messagebox.showerror(NOME_APP, "Alguns não foram apagados:\n\n" + "\n".join(falhas[:5]))

    def estado_todos():
        if not registros:
            return 0
        qtd = sum(1 for r in registros if r["id"] in marcados)
        return 0 if qtd == 0 else 1 if qtd == len(registros) else 2

    def alternar_todos():
        if estado_todos() == 1:
            marcados.clear()
        else:
            marcados.update(r["id"] for r in registros)
        atualizar_marcas()

    def alternar(r):
        if r["id"] in marcados:
            marcados.discard(r["id"])
        else:
            marcados.add(r["id"])
        atualizar_marcas()

    def atualizar_marcas():
        for r in registros:
            if r["id"] in linhas:
                redesenhar(r)
        desenhar_barra()

    # ---- icones desenhados direto na linha
    def i_pasta(c, x, y, cor, tag):
        c.create_polygon(x - 9, y - 6, x - 3, y - 6, x - 1, y - 4, x + 9, y - 4, x + 9, y + 7, x - 9, y + 7,
                         fill="", outline=cor, width=2, joinstyle="round", tags=(tag,))

    def i_lixeira(c, x, y, cor, tag):
        c.create_line(x - 8, y - 5, x + 8, y - 5, fill=cor, width=2, capstyle="round", tags=(tag,))
        c.create_line(x - 3, y - 5, x - 3, y - 8, x + 3, y - 8, x + 3, y - 5, fill=cor, width=2, tags=(tag,))
        c.create_polygon(x - 6, y - 3, x + 6, y - 3, x + 5, y + 8, x - 5, y + 8, fill="", outline=cor, width=2,
                         joinstyle="round", tags=(tag,))
        c.create_line(x - 2, y, x - 2, y + 5, fill=cor, width=2, tags=(tag,))
        c.create_line(x + 2, y, x + 2, y + 5, fill=cor, width=2, tags=(tag,))

    def i_link(c, x, y, cor, tag):
        import math

        def capsula(cx, cy):   # dois "elos" inclinados 45 graus
            a = math.radians(-45)
            ca, sa = math.cos(a), math.sin(a)
            pts = []
            for px, py in ((-6, -3.5), (0, -3.5), (6, -3.5), (6, 0), (6, 3.5), (0, 3.5), (-6, 3.5), (-6, 0)):
                pts += [cx + px * ca - py * sa, cy + px * sa + py * ca]
            c.create_polygon(pts, smooth=True, fill="", outline=cor, width=2, tags=(tag,))
        capsula(x - 3.5, y + 3.5)
        capsula(x + 3.5, y - 3.5)

    def i_estilo(c, x, y, cor, tag):
        c.create_text(x, y + 1, text="Aa", font=("Segoe UI Semibold", 12), fill=cor, tags=(tag,))

    def i_converter(c, x, y, cor, tag):
        c.create_text(x, y + 1, text="Pr", font=("Segoe UI Semibold", 11), fill=cor, tags=(tag,))

    dica_win = {"w": None}

    def mostrar_dica(widget, texto):
        esconder_dica()
        t = tk.Toplevel(root)
        t.wm_overrideredirect(True)
        t.configure(bg=COR["borda"])
        tk.Label(t, text=texto, font=f_status, bg=COR["linha2"], fg=COR["texto"], padx=P(8), pady=P(4)).pack(padx=P(1), pady=P(1))
        x, y = widget.winfo_pointerx() + 12, widget.winfo_pointery() + 16
        t.wm_geometry(f"+{x}+{y}")
        dica_win["w"] = t

    def esconder_dica():
        if dica_win["w"] is not None:
            dica_win["w"].destroy()
            dica_win["w"] = None

    def i_refazer(c, x, y, cor, tag):
        import math
        r0 = 7
        c.create_arc(x - r0, y - r0, x + r0, y + r0, start=50, extent=285, style="arc", outline=cor, width=2, tags=(tag,))
        t = math.radians(335)   # ponta da seta no fim do arco
        px, py = x + r0 * math.cos(t), y - r0 * math.sin(t)
        tx, ty = -math.sin(t), -math.cos(t)
        nx, ny = -ty, tx
        c.create_polygon(px + tx * 6, py + ty * 6, px - tx * 1 + nx * 5, py - ty * 1 + ny * 5,
                         px - tx * 1 - nx * 5, py - ty * 1 - ny * 5, fill=cor, outline=cor, tags=(tag,))

    def i_baixar(c, x, y, cor, tag):   # seta para baixo sobre uma bandeja
        c.create_line(x, y - 8, x, y + 3, fill=cor, width=2, capstyle="round", tags=(tag,))
        c.create_line(x - 5, y - 2, x, y + 4, x + 5, y - 2, fill=cor, width=2, capstyle="round", joinstyle="round", tags=(tag,))
        c.create_line(x - 8, y + 4, x - 8, y + 8, x + 8, y + 8, x + 8, y + 4, fill=cor, width=2, joinstyle="round", tags=(tag,))

    def i_parar(c, x, y, cor, tag):   # quadrado de "parar"
        c.create_polygon(x - 6, y - 6, x + 6, y - 6, x + 6, y + 6, x - 6, y + 6, fill="", outline=cor, width=2,
                         joinstyle="round", tags=(tag,))

    def pintar(c, tag, cor):
        for item in c.find_withtag(tag):
            tipo = c.type(item)
            if tipo == "line":
                c.itemconfig(item, fill=cor)
            elif tipo == "arc":
                c.itemconfig(item, outline=cor)
            elif tipo == "text":
                c.itemconfig(item, fill=cor)
            elif tipo == "polygon":
                c.itemconfig(item, outline=cor)
                if c.itemcget(item, "fill"):
                    c.itemconfig(item, fill=cor)

    def caixa(c, x, y, estado, tag, fundo):
        """Caixinha de marcar: estado 0 vazia, 1 marcada, 2 parcial."""
        c.create_rectangle(x - 15, y - 15, x + 15, y + 15, fill=fundo, outline="", tags=(tag, "hit"))
        if estado == 0:
            c.create_rectangle(x - 8, y - 8, x + 8, y + 8, fill=COR["campo"], outline=COR["fraco"], width=1, tags=(tag,))
        else:
            c.create_rectangle(x - 8, y - 8, x + 8, y + 8, fill=COR["destaque"], outline=COR["destaque"], tags=(tag,))
            if estado == 1:
                c.create_line(x - 4, y, x - 1, y + 4, x + 5, y - 4, fill="#ffffff", width=2, capstyle="round",
                              joinstyle="round", tags=(tag,))
            else:
                c.create_line(x - 4, y, x + 4, y, fill="#ffffff", width=2, capstyle="round", tags=(tag,))

    def clicavel(c, tag, cmd):
        c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))
        c.tag_bind(tag, "<Button-1>", lambda e: cmd())

    def desenhar_barra():
        larg = max(barra.winfo_width(), 400)
        barra.delete("all")
        sel = [r for r in registros if r["id"] in marcados]
        caixa(barra, 18, 22, estado_todos(), "b_todos", COR["fundo"])
        barra.create_text(40, 22, text="Selecionar todos", font=f_negrito, fill=COR["texto"], anchor="w", tags=("b_todos",))
        clicavel(barra, "b_todos", alternar_todos)
        if sel:
            barra.create_text(40 + med(f_negrito, "Selecionar todos") + 16, 22, text=f"{len(sel)} selecionados",
                              font=f_texto, fill=COR["fraco"], anchor="w")
        rot = f"Apagar selecionados ({len(sel)})" if sel else "Apagar selecionados"
        w = med(f_negrito, rot) + 52
        cor = COR["erro"] if sel else COR["apagado"]
        retangulo(barra, larg - w - 2, 3, larg - 2, 41, 12, fill=COR["linha"] if sel else COR["fundo"], outline=cor,
                  tags=("b_apagar",))
        i_lixeira(barra, larg - w + 22, 22, cor, "b_apagar")
        barra.create_text(larg - w + 40, 22, text=rot, font=f_negrito, fill=cor, anchor="w", tags=("b_apagar",))
        if sel:
            clicavel(barra, "b_apagar", apagar_varios)
    barra.bind("<Configure>", lambda e: desenhar_barra())

    ALTURA = 68

    def redesenhar(r):
        c = linhas[r["id"]]
        c.delete("all")
        larg = max(c.winfo_width(), 300)
        marcado = r["id"] in marcados
        retangulo(c, 1, 1, larg - 1, ALTURA - 1, 12, fill=COR["linha"], outline=COR["destaque"] if marcado else COR["borda"],
                  tags=("fundo",))
        caixa(c, 26, ALTURA / 2, 1 if marcado else 0, "i_sel", COR["linha"])
        clicavel(c, "i_sel", lambda rr=r: alternar(rr))
        ativo_link = str(r.get("link", "")).startswith(("http://", "https://"))
        livre = r["status"] in ("pronto", "erro", "interrompido") and not r.get("nota")
        rodando = not livre
        tem_link = bool(r.get("link"))
        pode_estilo = livre and r["status"] == "pronto" and r.get("modo") != "baixar" and bool(
            arquivos_para_refazer(r.get("arquivos"), r.get("link"), r.get("trecho")))
        pode_converter = livre and r["status"] == "pronto" and original_sem_conversao(r) is not None
        icones = [("i_lixo", i_lixeira, livre, apagar, "Apagar"),
                  ("i_pasta", i_pasta, r["status"] == "pronto", abrir, "Abrir a pasta"),
                  ("i_estilo", i_estilo, pode_estilo, restilizar, "Refazer a legenda com o estilo em uso"),
                  ("i_converter", i_converter, pode_converter, converter,
                   "Converter para o Premiere (cria uma cópia; o original não muda)"),
                  # um botao so: enquanto baixa, reinicia; depois de terminar (ou falhar), baixa de novo
                  ("i_baixar", i_refazer if rodando else i_baixar, tem_link, reiniciar if rodando else refazer,
                   "Reiniciar o download" if rodando else "Baixar de novo"),
                  ("i_parar", i_parar, rodando, parar, "Interromper"),
                  ("i_link", i_link, ativo_link, abrir_link, "Abrir o link")]
        x = larg - 34
        for tag, desenho, ativo, cmd, dica in icones:
            c.create_rectangle(x - 17, ALTURA / 2 - 17, x + 17, ALTURA / 2 + 17, fill=COR["linha"], outline="", tags=(tag, "hit"))
            desenho(c, x, ALTURA / 2, COR["fraco"] if ativo else COR["apagado"], tag)
            if ativo:
                c.tag_bind(tag, "<Enter>", lambda e, t=tag, cv=c, d=dica: (cv.configure(cursor="hand2"),
                                                                          pintar(cv, t, COR["destaque2"]), mostrar_dica(cv, d)))
                c.tag_bind(tag, "<Leave>", lambda e, t=tag, cv=c: (cv.configure(cursor=""), pintar(cv, t, COR["fraco"]),
                                                                  esconder_dica()))
                c.tag_bind(tag, "<Button-1>", lambda e, cm=cmd, rr=r: (esconder_dica(), cm(rr)))
            x -= 40
        x0 = 52
        espaco = x + 40 - 17 - 20 - x0
        titulo = r["titulo"]
        if med(f_linha, titulo) > espaco:   # corta o titulo (busca binaria: poucas medicoes por titulo)
            lo, hi = 0, len(titulo)
            while lo < hi:
                m = (lo + hi + 1) // 2
                if med(f_linha, titulo[:m] + "…") <= espaco:
                    lo = m
                else:
                    hi = m - 1
            titulo = titulo[:lo].rstrip(" -_") + "…"
        c.create_text(x0, 24, text=titulo, font=f_linha, fill=COR["texto"], anchor="w")
        cor = cor_status(r)
        c.create_oval(x0, 41, x0 + 8, 49, fill=cor, outline="")
        st = c.create_text(x0 + 16, 45, text=texto_status(r), font=f_status, fill=cor, anchor="w")
        if r["status"] == "erro":
            c.tag_bind(st, "<Button-1>", lambda e, rr=r: messagebox.showerror(NOME_APP, rr.get("erro", "")))
            c.tag_bind(st, "<Enter>", lambda e, cv=c: cv.configure(cursor="hand2"))
            c.tag_bind(st, "<Leave>", lambda e, cv=c: cv.configure(cursor=""))
        tx = x0 + 16 + med(f_status, texto_status(r)) + 16   # etiquetas depois do status
        for rot in etiquetas(r):
            w = med(f_tag, rot) + 14
            c.create_rectangle(tx, 37, tx + w, 53, fill=COR["fundo"], outline=COR["borda"])
            c.create_text(tx + w / 2, 45, text=rot, font=f_tag, fill=COR["fraco"])
            tx += w + 6
        if trabalhando(r) and not r["status"].startswith("Esperando"):
            c.create_rectangle(x0, ALTURA - 10, larg - 22, ALTURA - 8, fill=COR["borda"], outline="", tags=("trilho",))
            pct = pct_do(r)
            if pct is not None:   # download com porcentagem: barra que enche
                c.create_rectangle(x0, ALTURA - 10, x0 + (larg - 22 - x0) * pct / 100, ALTURA - 8,
                                   fill=COR["destaque2"], outline="", tags=("enche",))
            else:                 # sem porcentagem: faixa que corre
                c.create_rectangle(x0, ALTURA - 10, x0, ALTURA - 8, fill=COR["destaque2"], outline="", tags=("barra",))

    def desenhar(r):
        if r["id"] not in linhas:
            c = Tela(lista, height=ALTURA, bg=COR["fundo"], highlightthickness=0, bd=0)
            c.pack(fill="x", pady=(P(0), P(10)))
            linhas[r["id"]] = c
            c.bind("<Configure>", lambda e, rr=r: redesenhar(rr))
            c.bind("<Enter>", lambda e, cv=c: (cv.itemconfig("fundo", fill=COR["linha2"]), cv.itemconfig("hit", fill=COR["linha2"])))
            c.bind("<Leave>", lambda e, cv=c: (cv.itemconfig("fundo", fill=COR["linha"]), cv.itemconfig("hit", fill=COR["linha"])))
            filhos = [w for w in lista.pack_slaves() if w is not vazio and w is not c]   # na ordem em que aparecem
            if filhos:   # mais novo primeiro
                c.pack_configure(before=filhos[0])
        redesenhar(r)
        atualizar_vazio()
        desenhar_barra()

    def animar():
        algum = False
        for r in registros:
            c = linhas.get(r["id"])
            if c is not None and trabalhando(r) and c.find_withtag("barra"):
                algum = True
                larg = max(c.winfo_width(), 300)
                p = animacao.get(r["id"], 0) + 14
                if p > larg:
                    p = 52 - 90   # volta para antes do inicio: a faixa entra pela esquerda sem passar da trilha
                animacao[r["id"]] = p
                ini, fim = max(52, p), min(larg - 22, p + 90)
                if fim <= ini:
                    ini = fim = 52
                c.coords("barra", ini, ALTURA - 10, fim, ALTURA - 8)
        root.after(40 if algum else 250, animar)   # sem nada rodando, quase nao trabalha

    for r in registros:
        r.setdefault("id", str(id(r)))
    for r in registros[::-1]:
        desenhar(r)
    atualizar_vazio()

    # ---- processamento: um video por vez, os outros esperam na fila
    ETAPAS = (("Esperando", "Esperando a vez..."), ("Refazendo a legenda", "Refazendo a legenda..."), ("Baixando", "Baixando..."), ("Transcrevendo", "Transcrevendo..."), ("Idioma detectado", "Traduzindo..."),
              ("Traduzindo", "Traduzindo..."), ("Procurando legendas", "Analisando a tela..."), ("Colocando a legenda", "Colocando a legenda..."),
              ("Codificando", "Gerando o video..."), ("Convertendo para MP3", "Convertendo para MP3..."),
              ("Convertendo para o Premiere", "Convertendo para o Premiere..."))

    ativos = {}       # id -> Trabalho em andamento (para poder interromper)
    reinicio = set()  # ids a recolocar na fila assim que a interrupcao terminar

    def enfileirar(r):
        r["exec"] = r.get("exec", 0) + 1
        trabalhos.put((r, r["exec"]))

    def executar(r, trab):
        def log(msg, r=r):
            if trab.cancelado.is_set():
                raise Cancelado()
            gravar_log(f"[{time.strftime('%d/%m %H:%M:%S')}] {r['titulo'][:40]} | {msg}")
            if msg.startswith("Video: "):
                eventos.put((r, {"titulo": msg[7:]}))
            if msg.startswith(("Baixando", "Convertendo")) and "%" in msg:   # porcentagem na lista
                eventos.put((r, {"status": msg.strip().replace("Convertendo...", "Convertendo para o Premiere...")}))
                return
            for chave, rotulo in ETAPAS:
                if msg.strip().startswith(chave):
                    eventos.put((r, {"status": rotulo}))
        if r.get("acao") == "converter":
            try:
                origem = original_sem_conversao(r)
                if origem is None:
                    raise RuntimeError("nao encontrei o video original na pasta")
                with VEZ_PROC(lambda: eventos.put((r, {"status": "Esperando a vez..."}))):
                    copia = converter_copia(origem, log)
                eventos.put((r, {"status": "pronto", "acao": None, "copia": str(copia),
                                 "arquivos": list(r.get("arquivos") or []) + [str(copia)]}))
            except (Cancelado, Exception) as e:  # noqa: BLE001
                eventos.put((r, {"status": "pronto", "acao": None}))
                if not isinstance(e, Cancelado):
                    gravar_log(f"ERRO converter: {e}")
                    ui_fila.put(lambda m=str(e): messagebox.showerror(NOME_APP, "Não consegui converter:\n\n" + m))
            return
        if r.get("acao") == "restilizar":
            try:
                with VEZ_PROC(lambda: eventos.put((r, {"status": "Esperando a vez..."}))):
                    refazer_legenda(r.get("arquivos"), r.get("estilo"), log, r.get("link"), r.get("trecho"),
                                    r.get("estilo_v"))
                eventos.put((r, {"status": "pronto", "acao": None, "estilo_antigo": None, "nome_antigo": None}))
            except (Cancelado, Exception) as e:  # noqa: BLE001
                if not isinstance(e, Cancelado):
                    gravar_log(f"ERRO refazer legenda: {e}")
                eventos.put((r, {"status": "pronto", "acao": None,
                                 "estilo": r.pop("estilo_antigo", r.get("estilo")),
                                 "estilo_v": r.pop("estilo_v_antigo", r.get("estilo_v")),
                                 "nome_estilo": r.pop("nome_antigo", r.get("nome_estilo"))}))
                if not isinstance(e, Cancelado):
                    ui_fila.put(lambda m=str(e): messagebox.showerror(NOME_APP, "Não consegui refazer a legenda:\n\n" + m))
            return
        try:
            saida, arquivos, original = processar(r["link"], log, saida_base=r["base"], legendar=r.get("modo") != "baixar",
                                                  destino=r.get("lang", "pt"), trecho=r.get("trecho"),
                                                  estilo=r.get("estilo"), estilo_v=r.get("estilo_v"),
                                                  audio=bool(r.get("audio")), premiere=r.get("premiere", True),
                                                  altura=r.get("altura"))
            nome = Path(arquivos[0]).stem
            velhos = [a for a in r.pop("substituir", []) if a not in arquivos]   # "baixar de novo": tira os antigos
            if velhos:
                try:
                    apagar_arquivos(velhos, r["base"])
                except Exception as e:  # noqa: BLE001
                    gravar_log(f"AVISO nao apaguei os arquivos antigos: {e}")
            eventos.put((r, {"status": "pronto", "arquivos": arquivos, "pasta": None, "titulo": nome}))
            # depois de entregar: deixa o original no formato que o Premiere le sem datamosh
            if original is None:   # arquivo do proprio computador: nao ha original novo para preparar
                return
            eventos.put((r, {"nota": "preparando o original para o Premiere..."}))
            try:
                with VEZ_PROC():   # nao converte dois arquivos ao mesmo tempo (mas o proximo ja pode baixar)
                    novo = preparar_original(original, log)
                if novo and str(novo) not in arquivos:
                    eventos.put((r, {"arquivos": arquivos + [str(novo)]}))
            except Cancelado:
                gravar_log("Preparacao do original interrompida.")
            except Exception as e:  # noqa: BLE001
                gravar_log(f"AVISO preparar original: {e}")
            finally:
                eventos.put((r, {"nota": ""}))
        except Cancelado:
            eventos.put((r, {"status": "interrompido", "nota": ""}))
        except Exception as e:  # noqa: BLE001
            if trab.cancelado.is_set():   # o ffmpeg/yt-dlp morreu porque o usuario interrompeu
                eventos.put((r, {"status": "interrompido", "nota": ""}))
            else:
                gravar_log(f"ERRO: {e}")
                eventos.put((r, {"status": "erro", "erro": str(e)}))

    def trabalhador():
        while True:
            r, ficha = trabalhos.get()
            if ficha != r.get("exec") or r["status"] != "fila":
                continue   # foi interrompido ou reiniciado enquanto esperava na fila
            trab = Trabalho()
            ativos[r["id"]] = trab
            CTX.trabalho = trab
            try:
                if ficha == r.get("exec"):
                    executar(r, trab)
            finally:
                CTX.trabalho = None
                ativos.pop(r["id"], None)

    for _ in range(4):   # um baixa enquanto outro e processado (cada etapa pesada e feita uma por vez)
        threading.Thread(target=trabalhador, daemon=True).start()

    def buscar_titulo(r, prefixo):
        """Links do YouTube: troca o link pelo titulo do video assim que ele chegar (X e Instagram ganham o nome
        quando o programa decide o titulo, durante o processamento)."""
        if not re.search(r"(youtube\.com|youtu\.be)/", r["link"]):
            return
        rotulo = r["titulo"]

        def rodar():
            try:
                import urllib.parse
                import urllib.request
                u = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(r["link"], safe="")
                with urllib.request.urlopen(u, timeout=10) as resp:
                    t = " ".join((json.loads(resp.read().decode("utf-8")).get("title") or "").split())
                if t and r.get("titulo") == rotulo:   # so troca se o nome ainda e o link
                    eventos.put((r, {"titulo": prefixo + t}))
            except Exception as e:  # noqa: BLE001
                gravar_log(f"titulo do video nao encontrado ({str(e)[:80]}); mantendo o link")
        threading.Thread(target=rodar, daemon=True).start()

    def adicionar(link, modo, trecho, base=None, audio=False, premiere=True, altura=None):
        for k in ("h", "v"):
            if uso[k] not in estilos:
                uso[k] = next(iter(estilos))
        arquivo = Path(link).is_file()
        prefixo = f"[{mostrar_tempo(trecho[0])}–{mostrar_tempo(trecho[1])}] " if trecho else ""
        rotulo = prefixo + (Path(link).name if arquivo else (link if len(link) < 70 else link[:67] + "..."))
        r = {"id": f"{time.time():.6f}", "titulo": rotulo, "link": link,
             "base": base or pasta_var.get(), "status": "fila", "pasta": None, "modo": modo, "lang": idioma["v"],
             "trecho": trecho, "estilo": dict(estilos[uso["h"]]), "estilo_v": dict(estilos[uso["v"]]),
             "nome_estilo": uso["h"], "audio": audio, "premiere": premiere, "altura": altura}
        registros.insert(0, r)
        desenhar(r)
        enfileirar(r)
        if not arquivo:
            buscar_titulo(r, prefixo)

    def adicionar_link(modo="legendar"):
        link = campo.get().strip().strip('"')
        if not link:
            campo.focus()
            return
        if Path(link).is_file():
            aviso("Isso é um arquivo do seu computador. Use a aba \"Do computador\".")
            return
        ok, trecho = trecho_link["valor"]()
        if not ok:
            return
        i = combo_qual.current()
        altura = qual["alturas"][i] if 0 <= i < len(qual["alturas"]) and qual["link"] == link else 0
        concluir_link(link, modo, trecho, altura)

    def concluir_link(link, modo, trecho, altura):
        campo.delete(0, "end")
        trecho_link["fechar"]()
        so_audio = opc_audio["on"]
        adicionar(link, "baixar" if so_audio else modo, trecho, audio=so_audio, premiere=opc_premiere["on"],
                  altura=altura or None)
        if so_audio:   # vale so para este download: o proximo volta a ser video
            opc_audio["marcar"](False)
            atualizar_opcoes()
        qual["link"] = None
        qual["geracao"] += 1
        qual_padrao()
        tela.yview_moveto(0)   # mostra o video novo no topo da lista

    def adicionar_pc():
        arq = campo_pc.get().strip().strip('"')
        if not arq:
            escolher_arquivo()
            arq = campo_pc.get().strip().strip('"')
            if not arq:
                return
        if not Path(arq).is_file():
            aviso("Não encontrei esse arquivo. Use \"Escolher arquivo\".")
            return
        ok, trecho = trecho_pc["valor"]()
        if not ok:
            return
        campo_pc.delete(0, "end")
        trecho_pc["fechar"]()
        adicionar(str(Path(arq)), "legendar", trecho, base=str(Path(arq).parent))   # resultado ao lado do video
        tela.yview_moveto(0)

    def receber():
        try:
            while True:
                r, mud = eventos.get_nowait()
                r.update(mud)
                if r in registros:
                    desenhar(r)
                if r["status"] in ("pronto", "erro", "interrompido"):
                    salvar_lista()
                if r["status"] == "interrompido" and r["id"] in reinicio:   # "reiniciar" durante o download
                    reinicio.discard(r["id"])
                    r.update(status="fila", erro=None, nota="")
                    desenhar(r)
                    enfileirar(r)
        except queue.Empty:
            pass
        while True:
            try:
                ui_fila.get_nowait()()
            except queue.Empty:
                break
        root.after(200, receber)

    campo.bind("<Return>", lambda e: adicionar_link("legendar"))
    campo_pc.bind("<Return>", lambda e: adicionar_pc())
    def ao_fechar():
        guardar_janela()
        for t in list(ativos.values()):   # nao deixa ffmpeg/yt-dlp rodando sem janela
            t.cancelar()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", ao_fechar)
    mostrar_aba("baixar")
    receber()
    animar()
    root.after(2500, verificar_atualizacao)
    if not ler_config().get("configurado") and not os.environ.get("GEMINI_API_KEY"):   # primeira vez neste computador
        root.after(500, lambda: abrir_configuracao(True))
    root.mainloop()


if __name__ == "__main__":
    limpar_cache()
    limpar_temporarios()
    if len(sys.argv) > 1:
        processar(" ".join(sys.argv[1:]).strip('"'))
    else:
        janela()
