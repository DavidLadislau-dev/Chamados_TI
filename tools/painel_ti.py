#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
painel_ti.py — registra subtarefas e observações nas atividades do Painel de T.I.
(aba Atividades do Sistema de Chamados, Firestore `chamados-ti-ng`).

Uso (PowerShell ou Bash):
  painel-ti listar [--todas]
  painel-ti ver -p "Velkar"
  painel-ti registrar -p "Velkar" -t "Corrigido o mapa lento (/posicoes/atual)" [--pendente] [--criar] [--sem-ruflo]
  painel-ti obs -p "Velkar" -t "Deploy feito na norte-vm" [--autor DAVID]

Regras que espelham o painel (painel_html.html):
  - subtarefa = {texto, feito, em: "DD/MM/AAAA, HH:MM:SS"}   (new Date().toLocaleString('pt-BR'))
  - ao mudar a quantidade de subtarefas, o porte é automático: 1-2 Pequeno (1 pt), 3-6 Médio (3 pts), 7+ Grande (8 pts)
  - observação = {texto, autor, em}
  - atualizadoEm/criadoEm = serverTimestamp

Também espelha a subtarefa na memória do Ruflo (namespace = slug do projeto), se o `ruflo` estiver no PATH.
Sem dependências além da stdlib.
"""
import argparse
import datetime as _dt
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PROJECT = "chamados-ti-ng"
API_KEY = "AIzaSyAGkaCk_ot9tqgeH1c1E14ZAt0r2e_u5N8"  # chave web pública (a mesma do painel_html.html)
DOCS = f"projects/{PROJECT}/databases/(default)/documents"
BASE = f"https://firestore.googleapis.com/v1/{DOCS}"
COMMIT = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents:commit"

RESPONSAVEL_PADRAO = "DAVID"
PORTES = {"Pequeno": 1, "Médio": 3, "Grande": 8}

# Banco de memória compartilhado do Ruflo (um só pra todos os projetos, já que o claude roda da home)
RUFLO_DB = os.environ.get(
    "CLAUDE_FLOW_DB_PATH",
    os.path.join(os.path.expanduser("~"), "ruflo-lab", ".swarm", "memory.db"),
)

# nome de repo / apelido  ->  título exato da atividade no painel
ALIASES = {
    "frota_norte": "Velkar (Frota Norte)", "frota norte": "Velkar (Frota Norte)", "velkar": "Velkar (Frota Norte)",
    "jarvis": "Jarvis",
    "sexta-feira": "Sexta-Feira (F.R.I.D.A.Y.)", "sexta feira": "Sexta-Feira (F.R.I.D.A.Y.)", "friday": "Sexta-Feira (F.R.I.D.A.Y.)",
    "chamados_ti": "Chamados T.I", "chamados ti": "Chamados T.I", "painel ti": "Chamados T.I", "painel de ti": "Chamados T.I",
    "juridico-norte": "Acompanhamento de Processos Judiciais", "juridico": "Acompanhamento de Processos Judiciais", "painel juridico": "Acompanhamento de Processos Judiciais",
    "zpro": "Zpro - Migração", "z-pro": "Zpro - Migração", "sdr chatflow": "Zpro - Migração",
    "campanhas-email": "Email Marketing", "campanhas email": "Email Marketing", "email marketing": "Email Marketing",
    "bi-manutencao": "BI de Manutenção", "bi manutencao": "BI de Manutenção",
    "tally-ng": "Tally NG", "tally": "Tally NG", "contagem": "Tally NG",
    "preventiva-filtros": "Preventiva Filtros", "preventiva": "Preventiva Filtros",
    "manutencao-predial": "Manutenção Predial", "manutencao predial": "Manutenção Predial",
    "site-norte-geradores": "Site Norte Geradores", "site institucional": "Site Norte Geradores",
    "analista-pedidos": "Analise de pedido de compra", "analista de pedidos": "Analise de pedido de compra",
    "rateio-endividamento": "BI de endividamento", "rateio": "BI de endividamento",
    "mapa-ti": "Mapear maquinas da rede", "mapa ti": "Mapear maquinas da rede",
    "painel-bi": "Painel de Softwares — Norte Geradores", "norte apps": "Painel de Softwares — Norte Geradores", "painel bi": "Painel de Softwares — Norte Geradores",
    "analise-sisloc": "Analise de Produção SISLOC", "analise sisloc": "Analise de Produção SISLOC",
    "prospeccao": "Prospecção SDR", "prospeccao sdr": "Prospecção SDR",
    "catalogo": "Catálago de equipamentos disponiveis - Automatizado", "catalogo de equipamentos": "Catálago de equipamentos disponiveis - Automatizado",
    "bi-comercial": "BI Comercial", "bi comercial": "BI Comercial",
    "bi-faturamento": "BI Faturamento", "faturamento mensal": "BI Faturamento",
    "assistente-compras": "Assistente de Compras", "assistente de compras": "Assistente de Compras",
    "ebooks": "Criar e-books de treinamentos do comercial", "panda ebooks": "Criar e-books de treinamentos do comercial",
    "gerador de proposta": "Gerador de Proposta / Simulador de Cotação",
    "auditoria de seguranca": "Fazer auditoria de segurança na rede", "auditoria": "Fazer auditoria de segurança na rede",
    "bitrix": "Gerenciador de Tarefas / BITRIX",
}


# ---------------------------------------------------------------- utilidades
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(s)).strip("-")[:40] or "projeto"


def agora_str() -> str:
    return _dt.datetime.now().strftime("%d/%m/%Y, %H:%M:%S")


def hoje_str() -> str:
    return _dt.date.today().isoformat()


def porte_auto(n: int):
    if n <= 0:
        return None
    if n <= 2:
        return "Pequeno"
    if n <= 6:
        return "Médio"
    return "Grande"


def _novo_id(n=20) -> str:
    alf = string.ascii_letters + string.digits
    return "".join(random.choice(alf) for _ in range(n))


# ------------------------------------------------------- Firestore REST codec
def enc(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [enc(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: enc(x) for k, x in v.items()}}}
    raise TypeError(f"tipo não suportado: {type(v)}")


def dec(f):
    if f is None:
        return None
    if "stringValue" in f:
        return f["stringValue"]
    if "booleanValue" in f:
        return f["booleanValue"]
    if "integerValue" in f:
        return int(f["integerValue"])
    if "doubleValue" in f:
        return f["doubleValue"]
    if "nullValue" in f:
        return None
    if "timestampValue" in f:
        return f["timestampValue"]
    if "arrayValue" in f:
        return [dec(x) for x in f["arrayValue"].get("values", [])]
    if "mapValue" in f:
        return {k: dec(x) for k, x in f["mapValue"].get("fields", {}).items()}
    return f


def _http(method: str, url: str, body=None):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}key={API_KEY}", method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"❌ Firestore {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"❌ Sem conexão com o Firestore: {e.reason}")


# ----------------------------------------------------------------- atividades
def listar_atividades():
    out, token = [], None
    while True:
        url = f"{BASE}/atividades?pageSize=300"
        if token:
            url += f"&pageToken={urllib.parse.quote(token)}"
        d = _http("GET", url)
        for doc in d.get("documents", []):
            f = doc.get("fields", {})
            out.append({
                "id": doc["name"].split("/")[-1],
                "titulo": dec(f.get("titulo")) or "",
                "status": dec(f.get("status")) or "",
                "responsavel": dec(f.get("responsavel")) or "",
                "porte": dec(f.get("porte")) or "",
                "subtarefas": dec(f.get("subtarefas")) or [],
                "observacoes": dec(f.get("observacoes")) or [],
            })
        token = d.get("nextPageToken")
        if not token:
            return out


def achar(projeto: str, ativs):
    q = _norm(projeto)
    alvo = ALIASES.get(q)
    if alvo:
        q = _norm(alvo)
    exatas = [a for a in ativs if _norm(a["titulo"]) == q]
    if len(exatas) == 1:
        return exatas[0]
    parciais = [a for a in ativs if q in _norm(a["titulo"]) or _norm(a["titulo"]) in q]
    if len(parciais) == 1:
        return parciais[0]
    if len(parciais) > 1:
        # prefere a que não está concluída
        abertas = [a for a in parciais if a["status"] != "Concluído"]
        if len(abertas) == 1:
            return abertas[0]
        nomes = " | ".join(a["titulo"] for a in parciais)
        raise SystemExit(f"⚠️ '{projeto}' bate com mais de uma atividade: {nomes}. Use o título exato.")
    return None


def _write_update(doc_id: str, campos: dict, transforms: list, precond=None):
    write = {
        "update": {"name": f"{DOCS}/atividades/{doc_id}", "fields": {k: enc(v) for k, v in campos.items()}},
        "updateMask": {"fieldPaths": list(campos.keys())},
        "updateTransforms": transforms,
    }
    if precond:
        write["currentDocument"] = precond
    return _http("POST", COMMIT, {"writes": [write]})


def registrar(projeto: str, texto: str, feito=True, responsavel=RESPONSAVEL_PADRAO, criar=False, ruflo=True):
    ativs = listar_atividades()
    a = achar(projeto, ativs)
    sub = {"texto": texto, "feito": bool(feito), "em": agora_str()}
    if a is None:
        if not criar:
            raise SystemExit(
                f"❌ Nenhuma atividade no painel bate com '{projeto}'. "
                f"Rode `painel-ti listar --todas` pra ver os títulos, ou repita com --criar pra criar essa atividade."
            )
        doc_id = _novo_id()
        campos = {
            "titulo": projeto, "status": "Em andamento", "feito": False,
            "responsavel": responsavel, "prioridade": "Média", "info": "", "prazo": "",
            "subtarefas": [sub], "observacoes": [],
            "porte": "Pequeno", "pontos": 1, "porteDefinidoEm": hoje_str(),
        }
        transforms = [
            {"fieldPath": "criadoEm", "setToServerValue": "REQUEST_TIME"},
            {"fieldPath": "atualizadoEm", "setToServerValue": "REQUEST_TIME"},
        ]
        _write_update(doc_id, campos, transforms, precond={"exists": False})
        print(f"🆕 Atividade '{projeto}' criada no painel (responsável {responsavel}) com a 1ª subtarefa.")
        titulo = projeto
        n = 1
    else:
        n = len(a["subtarefas"]) + 1
        campos = {}
        novo = porte_auto(n)
        if novo and a["porte"] != novo:
            campos.update({"porte": novo, "pontos": PORTES[novo], "porteDefinidoEm": hoje_str()})
        transforms = [
            {"fieldPath": "subtarefas", "appendMissingElements": {"values": [enc(sub)]}},
            {"fieldPath": "atualizadoEm", "setToServerValue": "REQUEST_TIME"},
        ]
        _write_update(a["id"], campos, transforms)
        titulo = a["titulo"]
        aviso = "  (atividade está marcada como Concluído no painel)" if a["status"] == "Concluído" else ""
        print(f"✅ Subtarefa registrada em '{titulo}': {texto}  [{n} subtarefas, porte {novo}]{aviso}")

    if ruflo:
        _espelhar_ruflo(titulo, texto)
    return 0


def obs(projeto: str, texto: str, autor=RESPONSAVEL_PADRAO):
    ativs = listar_atividades()
    a = achar(projeto, ativs)
    if a is None:
        raise SystemExit(f"❌ Nenhuma atividade no painel bate com '{projeto}'.")
    o = {"texto": texto, "autor": autor, "em": agora_str()}
    transforms = [
        {"fieldPath": "observacoes", "appendMissingElements": {"values": [enc(o)]}},
        {"fieldPath": "atualizadoEm", "setToServerValue": "REQUEST_TIME"},
    ]
    _write_update(a["id"], {}, transforms)
    print(f"📝 Observação registrada em '{a['titulo']}' por {autor}.")
    return 0


def ver(projeto: str):
    a = achar(projeto, listar_atividades())
    if a is None:
        raise SystemExit(f"❌ Nenhuma atividade no painel bate com '{projeto}'.")
    feitas = sum(1 for s in a["subtarefas"] if s.get("feito"))
    print(f"{a['titulo']}  ·  {a['status']}  ·  👤 {a['responsavel'] or '—'}  ·  porte {a['porte'] or '—'}  ·  {feitas}/{len(a['subtarefas'])} subtarefas")
    for s in a["subtarefas"]:
        print(f"  [{'x' if s.get('feito') else ' '}] {s.get('texto','')}   ({s.get('em','')})")
    if a["observacoes"]:
        print("  Observações:")
        for o in a["observacoes"]:
            print(f"   · {o.get('autor','')}: {o.get('texto','')}   ({o.get('em','')})")
    return 0


def listar(todas=False):
    ativs = listar_atividades()
    ordem = {"Em andamento": 0, "Planejado": 1, "Pausado": 2, "Concluído": 3}
    ativs.sort(key=lambda a: (ordem.get(a["status"], 9), _norm(a["titulo"])))
    print(f"{'STATUS':<13} {'RESPONSÁVEL':<16} {'SUB':>7}  TÍTULO")
    for a in ativs:
        if not todas and a["status"] == "Concluído":
            continue
        feitas = sum(1 for s in a["subtarefas"] if s.get("feito"))
        print(f"{a['status']:<13} {a['responsavel'][:16]:<16} {feitas:>3}/{len(a['subtarefas']):<3}  {a['titulo']}")
    if not todas:
        print("(concluídas ocultas — use --todas)")
    return 0


# --------------------------------------------------------------- ruflo mirror
def _espelhar_ruflo(titulo: str, texto: str):
    exe = shutil.which("ruflo")
    if not exe:
        return
    env = dict(os.environ)
    env.setdefault("CLAUDE_FLOW_ENABLE_NATIVE_BRIDGE_ON_WINDOWS", "1")
    env.setdefault("RUFLO_DAEMON_AUTOSTART", "0")
    env.setdefault("npm_config_update_notifier", "false")
    if os.path.exists(RUFLO_DB):
        env.setdefault("CLAUDE_FLOW_DB_PATH", RUFLO_DB)
    ns = _slug(titulo)
    key = "sub-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            [exe, "memory", "store", "-n", ns, "-k", key, "-v", f"[{hoje_str()}] {texto}"],
            env=env, capture_output=True, text=True, timeout=90, creationflags=flags,
        )
        if r.returncode == 0 and "stored" in (r.stdout + r.stderr).lower():
            print(f"🧠 Espelhado na memória do Ruflo (namespace {ns}).")
        else:
            print(f"   (ruflo não gravou: {(r.stderr or r.stdout).strip().splitlines()[-1][:120] if (r.stderr or r.stdout).strip() else 'sem saída'})")
    except Exception as e:  # nunca derruba o registro no painel
        print(f"   (ruflo indisponível: {e})")


# ----------------------------------------------------------------------- main
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="painel-ti", description="Subtarefas e observações no Painel de T.I. (aba Atividades)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("listar", help="lista as atividades do painel")
    p.add_argument("--todas", action="store_true", help="inclui as concluídas")

    p = sp.add_parser("ver", help="mostra uma atividade com suas subtarefas")
    p.add_argument("-p", "--projeto", required=True)

    p = sp.add_parser("registrar", help="adiciona uma subtarefa (feita) na atividade do projeto")
    p.add_argument("-p", "--projeto", required=True, help="nome do projeto/repo ou título da atividade")
    p.add_argument("-t", "--texto", required=True, help="o que foi feito, em uma linha")
    p.add_argument("--pendente", action="store_true", help="registra como NÃO feita")
    p.add_argument("--responsavel", default=RESPONSAVEL_PADRAO)
    p.add_argument("--criar", action="store_true", help="cria a atividade se não existir")
    p.add_argument("--sem-ruflo", action="store_true", help="não espelha na memória do Ruflo")

    p = sp.add_parser("obs", help="adiciona uma observação na atividade")
    p.add_argument("-p", "--projeto", required=True)
    p.add_argument("-t", "--texto", required=True)
    p.add_argument("--autor", default=RESPONSAVEL_PADRAO)

    a = ap.parse_args(argv)
    if a.cmd == "listar":
        return listar(a.todas)
    if a.cmd == "ver":
        return ver(a.projeto)
    if a.cmd == "registrar":
        return registrar(a.projeto, a.texto, feito=not a.pendente, responsavel=a.responsavel, criar=a.criar, ruflo=not a.sem_ruflo)
    if a.cmd == "obs":
        return obs(a.projeto, a.texto, a.autor)
    return 1


if __name__ == "__main__":
    sys.exit(main())
