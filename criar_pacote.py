# -*- coding: utf-8 -*-
"""Gera o zip do FlowCaption para enviar a outra pessoa, SEM nada seu (cookies, chave, config, log, cache)."""
import os, re, sys, zipfile, time
from pathlib import Path

APP = Path(__file__).resolve().parent
RAIZ = "FlowCaption"
SAIDA = APP.parent / "FlowCaption_para_enviar.zip"

# Só entra o que está nesta lista (allowlist). Qualquer outra coisa na pasta é ignorada.
ARQUIVOS = ["legendar.py", "FlowCaption.bat", "INSTALAR.bat", "requirements.txt",
            "flowcaptions-v2.ico", "flowcaptions-v2.png"]
LEIAME_AMIGO = APP / "LEIA-ME_AMIGO.txt"      # vira LEIA-ME.txt dentro do zip
PASTAS = {"python": {"pular_dirs": {"__pycache__", "Scripts"}},
          "ffmpeg": {"so_arquivos": {"ffmpeg.exe", "ffprobe.exe"}}}
SEGREDO_NOME = re.compile(r"cookie|\.env|^env(\.txt)?$|config\.json|\.log$|^cache$", re.I)

# Padrões FORTES: valem em qualquer arquivo (inclusive binários).
FORTES = {
    "chave do Gemini": rb"AIza[0-9A-Za-z_\-]{35}",
    "GEMINI_API_KEY com valor": rb"GEMINI_API_KEY\s*=\s*[A-Za-z0-9_\-]{20,}",
    "arquivo de cookies": rb"# Netscape HTTP Cookie File",
    "cookie SAPISID": rb"SAPISID",
    "cookie __Secure-1PSID": rb"__Secure-1PSID",
    "cookie sessionid": rb"sessionid\t",
    "pasta do usuário": rb"[A-Za-z]:[\\/]+Users[\\/]+Iago",
    "pasta do programa": re.escape(str(APP).encode("utf-8", "ignore")),
    "pasta do programa (/)": re.escape(str(APP).replace("\\", "/").encode("utf-8", "ignore")),
}
# Padrão FRACO: só em arquivos de texto (em binários "Iago" aparece por acaso).
FRACOS = {"nome Iago": rb"Iago"}
EXT_TEXTO = {".py", ".txt", ".json", ".bat", ".cfg", ".ini", ".pth", ".md", ".toml", ".csv", ".cmd", ".ps1",
             ".yml", ".yaml", ".xml", ".html", ".rst", ".sh", ""}
RX_F = re.compile(b"|".join(b"(?P<g%d>%s)" % (i, p) for i, p in enumerate(FORTES.values())))
# Dentro de python/ e ffmpeg/ (código de terceiros) só valem sinais pessoais; chaves públicas e o texto
# "Netscape HTTP Cookie File" fazem parte dessas bibliotecas.
PESSOAIS = {k: FORTES[k] for k in ("pasta do usuário", "pasta do programa", "pasta do programa (/)")}
RX_P = re.compile(b"|".join(b"(?P<g%d>%s)" % (i, p) for i, p in enumerate(PESSOAIS.values())))
RX_W = re.compile(b"|".join(b"(?P<g%d>%s)" % (i, p) for i, p in enumerate(FRACOS.values())))
LIMITE_SCAN = 200 * 1024 * 1024
IGNORAR_SCAN = re.compile(r"(^|/)python/(Lib/site-packages/(.*/)?(METADATA|RECORD|LICENSE.*|AUTHORS.*)|LICENSE.*)$", re.I)
SENSIVEL = {"SEU segredo (valor exato)", "chave do Gemini", "GEMINI_API_KEY com valor", "cookie SAPISID", "cookie __Secure-1PSID", "cookie sessionid"}


def segredos_reais():
    """Valores EXATOS da sua chave e dos seus cookies, lidos só na memória (nunca impressos)."""
    vals = set()
    for n in (".env", ".env.txt", "env", "env.txt"):
        p = APP / n
        if p.is_file():
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "=" in ln:
                    v = ln.split("=", 1)[1].strip().strip("\"'")
                    if len(v) >= 16:
                        vals.add(v.encode())
    for p in APP.glob("*cookies*.txt"):
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            c = ln.split("\t")
            if len(c) >= 7 and len(c[6].strip()) >= 16:
                vals.add(c[6].strip().encode())
    return vals


def coletar():
    itens = []   # (caminho_no_disco, caminho_no_zip)
    for n in ARQUIVOS:
        p = APP / n
        if p.is_file():
            itens.append((p, f"{RAIZ}/{n}"))
        elif n in ("legendar.py", "FlowCaption.bat"):
            sys.exit(f"ERRO: falta {n} na pasta do programa.")
    if LEIAME_AMIGO.is_file():
        itens.append((LEIAME_AMIGO, f"{RAIZ}/LEIA-ME.txt"))
    for pasta, regras in PASTAS.items():
        base = APP / pasta
        if not base.is_dir():
            print(f"AVISO: pasta '{pasta}' não existe; o amigo terá de rodar INSTALAR.bat.")
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in regras.get("pular_dirs", ()) and "~" not in d]
            for f in fns:
                if f.endswith(".pyc") or SEGREDO_NOME.search(f):
                    continue
                if "so_arquivos" in regras and f.lower() not in regras["so_arquivos"] and Path(dp) == base:
                    continue
                p = Path(dp) / f
                itens.append((p, f"{RAIZ}/" + p.relative_to(APP).as_posix()))
    return itens


def varrer(itens):
    global REAIS
    REAIS = segredos_reais()
    achados = []
    n = len(itens)
    for i, (p, z) in enumerate(itens, 1):
        if i % 1000 == 0:
            print(f"  varrendo {i}/{n}...", flush=True)
        if IGNORAR_SCAN.search(z):
            continue
        try:
            if p.stat().st_size > LIMITE_SCAN:
                continue
            dado = p.read_bytes()
        except OSError:
            continue
        biblioteca = z.startswith((f"{RAIZ}/python/", f"{RAIZ}/ffmpeg/"))
        for v in REAIS:
            if v in dado:
                achados.append((z, "SEU segredo (valor exato)", ""))
                break
        else:
            v = None
        if v is not None:
            continue
        for rx, nomes, texto_only in ((RX_P if biblioteca else RX_F, list(PESSOAIS if biblioteca else FORTES), False),
                                      (RX_W, list(FRACOS), True)):
            if texto_only and p.suffix.lower() not in EXT_TEXTO:
                continue
            m = rx.search(dado)
            if m:
                nome = nomes[int(m.lastgroup[1:])]
                ctx = ""
                if nome not in SENSIVEL:   # nunca grava chave/cookie no relatório
                    ctx = dado[max(0, m.start() - 40):m.end() + 40].decode("latin1", "replace").replace("\n", " ")
                achados.append((z, nome, ctx))
                break
    return achados


def main():
    print("Montando a lista de arquivos...")
    itens = coletar()
    print(f"{len(itens)} arquivos. Procurando dados pessoais...")
    achados = varrer(itens)
    if achados:
        print("\nPACOTE NÃO CRIADO: encontrei possíveis dados pessoais nestes arquivos:")
        rel = APP / "relatorio_pacote.txt"
        linhas = [f"{z}  [{nome}]  {ctx}" for z, nome, ctx in achados]
        rel.write_text("\n".join(linhas), encoding="utf-8")
        for z, nome, _ in achados[:40]:
            print(f"  - {z}  [{nome}]")
        print(f"\n{len(achados)} arquivo(s). Lista completa em: {rel}")
        print("Mande esse arquivo (relatorio_pacote.txt) para o Claude.")
        sys.exit(2)
    if SAIDA.exists():
        SAIDA.unlink()
    total = sum(p.stat().st_size for p, _ in itens)
    feito = 0
    t0 = time.time()
    with zipfile.ZipFile(SAIDA, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
        for i, (p, z) in enumerate(itens, 1):
            zf.write(p, z)
            feito += p.stat().st_size
            if i % 200 == 0 or i == len(itens):
                print(f"  {feito*100//max(total,1)}%  ({i}/{len(itens)})", flush=True)
    print(f"\nPronto: {SAIDA}  ({SAIDA.stat().st_size/1e9:.2f} GB, {time.time()-t0:.0f}s)")
    print("Esse zip não contém seus cookies, sua chave do Gemini, config, log nem cache.")


if __name__ == "__main__":
    main()
