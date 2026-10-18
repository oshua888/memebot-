#!/usr/bin/env python3
"""
MemeScanner — scanner de meme coins + simulateur d'achat (paper trading).

AVERTISSEMENT : aucun outil ne peut prédire quel token va "exploser".
Ce programme filtre les tokens les plus risqués et classe les autres selon
leur dynamique du moment. Il trade en ARGENT FICTIF uniquement.

Sources de données (publiques, sans clé API) :
  - DexScreener   : https://docs.dexscreener.com/api/reference
  - GeckoTerminal : https://www.geckoterminal.com/dex-api
  - RugCheck      : https://api.rugcheck.xyz (Solana uniquement)

Usage :
  python3 memescanner.py scan                 # affiche le classement
  python3 memescanner.py bot                  # boucle : scan + achats/ventes simulés
  python3 memescanner.py portfolio            # état des portefeuilles simulés
  python3 memescanner.py analyse              # quelles mesures annonçaient les gains ?
  python3 memescanner.py reset                # remet tout à zéro
  ajouter --demo pour tester avec des données inventées (sans internet)
"""
import argparse
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- paramètres
CONFIG = {
    "chain": "solana",
    # filtres
    "min_liquidity_usd": 25_000,     # en dessous : trop facile à manipuler
    "min_age_hours": 2,              # les tout premiers instants = zone de rug
    "max_age_hours": 24 * 14,
    "min_volume_h24": 50_000,
    "max_fdv_to_liquidity": 40,      # FDV énorme vs liquidité = prix fictif
    "require_rugcheck": True,        # Solana : rejeter si RugCheck ne répond pas
    "min_lp_locked_pct": 80,         # liquidité verrouillée ou brûlée
    "max_top10_pct": 35,             # part détenue par les 10 plus gros portefeuilles
    "min_score_to_buy": 60,
    # portefeuille simulé
    "start_balance_usd": 1000.0,
    "position_size_pct": 2.0,        # % du capital par position
    "max_open_positions": 5,
    "stop_loss_pct": -30.0,
    "tp_ladder": [[50, 0.33], [100, 0.5]],  # [gain %, part du reste vendue]
    "trailing_stop_pct": 25.0,       # après la 1re prise de bénéfice
    "max_hold_hours": 12,            # vente si rien ne s'est passé
    "missing_cycles_writeoff": 5,    # token introuvable N fois de suite = perdu
    # coûts réalistes : frais fixes + impact sur le prix selon la liquidité
    "base_fee_pct": 1.0,             # frais DEX + réseau + priorité
    "impact_mult": 2.0,              # pool x*y=k : impact ≈ 2 × mise / liquidité
    "scan_interval_s": 60,
}

# Stratégies simulées en parallèle, sur les mêmes tokens, pour comparer.
# "hasard" ignore le score : si les autres ne font pas mieux, le score ne sert à rien.
STRATEGIES = {
    "prudente": {"min_score_to_buy": 70, "stop_loss_pct": -20.0,
                 "tp_ladder": [[30, 0.5], [60, 0.5]], "trailing_stop_pct": 15.0,
                 "position_size_pct": 1.0, "max_hold_hours": 8},
    "actuelle": {},
    "agressive": {"min_score_to_buy": 50, "stop_loss_pct": -40.0,
                  "tp_ladder": [[100, 0.33], [200, 0.5]], "trailing_stop_pct": 35.0,
                  "position_size_pct": 3.0, "max_hold_hours": 24},
    "hasard": {"random": True},
}
for _name in STRATEGIES:
    STRATEGIES[_name]["name"] = _name

DATA_DIR = Path(__file__).resolve().parent
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
JOURNAL_FILE = DATA_DIR / "journal.csv"
TRADES_FILE = DATA_DIR / "trades.csv"        # un trade fermé par ligne + ses mesures
STATUS_FILE = DATA_DIR / "status.json"
LOCK_FILE = DATA_DIR / ".bot.lock"

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
GECKO_NET = {"solana": "solana", "ethereum": "eth", "bsc": "bsc", "base": "base"}
RUGCHECK = "https://api.rugcheck.xyz/v1"

FEATURES = ["score", "liq", "fdv_liq", "age_h", "turnover", "buy_ratio",
            "m5", "h1", "h6", "boost"]


# ---------------------------------------------------------------- réseau
def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "memescanner/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_candidates(chain):
    """Adresses à étudier -> ensemble des sources qui les citent."""
    found = {}

    def add(addr, src):
        if addr:
            found.setdefault(addr, set()).add(src)

    # DexScreener : tokens « boostés » (publicité payée) et profils récents
    for path, src in (("/token-boosts/latest/v1", "boost"), ("/token-boosts/top/v1", "boost"),
                      ("/token-profiles/latest/v1", "profil")):
        try:
            for item in get_json(DEX + path):
                if item.get("chainId") == chain:
                    add(item.get("tokenAddress"), src)
        except Exception as e:
            print(f"  [!] dexscreener {path} : {e}", file=sys.stderr)
    # GeckoTerminal : pools en tendance et nouvelles pools (activité naturelle)
    net = GECKO_NET.get(chain)
    for path, src in (("trending_pools", "tendance"), ("new_pools", "nouveau")):
        if not net:
            break
        try:
            for pool in get_json(f"{GECKO}/networks/{net}/{path}?page=1").get("data", []):
                tid = (((pool.get("relationships") or {}).get("base_token") or {})
                       .get("data") or {}).get("id", "")
                add(tid.split("_", 1)[-1] if "_" in tid else None, src)
        except Exception as e:
            print(f"  [!] geckoterminal {path} : {e}", file=sys.stderr)
    return found


def fetch_pairs(chain, addrs):
    """Meilleure paire (la plus liquide) pour chaque token."""
    best = {}
    addrs = list(addrs)
    for i in range(0, len(addrs), 30):  # l'API accepte 30 adresses max
        try:
            pairs = get_json(f"{DEX}/tokens/v1/{chain}/{','.join(addrs[i:i+30])}")
        except Exception as e:
            print(f"  [!] tokens : {e}", file=sys.stderr)
            continue
        for p in pairs or []:
            addr = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if addr not in best or liq > ((best[addr].get("liquidity") or {}).get("usd") or 0):
                best[addr] = p
    return list(best.values())


def rugcheck(addr, cfg):
    """Contrôles anti-arnaque. Retourne (motifs de rejet, infos) ; None si indisponible."""
    try:
        r = get_json(f"{RUGCHECK}/tokens/{addr}/report")
    except Exception:
        return None, {}
    reasons = []
    if r.get("mintAuthority"):
        reasons.append("peut créer des tokens")
    if r.get("freezeAuthority"):
        reasons.append("peut bloquer les ventes")
    for risk in r.get("risks") or []:
        if risk.get("level") == "danger":
            reasons.append(risk.get("name", "risque"))
    locked = [((m.get("lp") or {}).get("lpLockedPct")) for m in r.get("markets") or []]
    locked = [x for x in locked if isinstance(x, (int, float))]
    lp = max(locked) if locked else None
    if lp is not None and lp < cfg["min_lp_locked_pct"]:
        reasons.append(f"liquidité non verrouillée ({lp:.0f}%)")
    known = r.get("knownAccounts") or {}
    pools = {a for a, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
    holders = [h for h in r.get("topHolders") or []
               if h.get("address") not in pools and h.get("owner") not in pools]
    top10 = sum(h.get("pct") or 0 for h in holders[:10]) if holders else None
    if top10 is not None and top10 > cfg["max_top10_pct"]:
        reasons.append(f"top 10 détient {top10:.0f}%")
    return reasons, {"lp_locked": lp, "top10": top10, "rug_score": r.get("score")}


# ---------------------------------------------------------------- analyse
def analyse(pair, cfg, sources=(), use_rugcheck=True):
    """Renvoie un dict avec score 0-100, mesures et raisons de rejet éventuelles."""
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    fdv = pair.get("fdv") or pair.get("marketCap") or 0
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    txh1 = (pair.get("txns") or {}).get("h1") or {}
    created = pair.get("pairCreatedAt") or 0
    age_h = (time.time() * 1000 - created) / 3_600_000 if created else 0

    rejects = []
    if liq < cfg["min_liquidity_usd"]:
        rejects.append("liquidité faible")
    if age_h < cfg["min_age_hours"]:
        rejects.append("trop récent")
    if age_h > cfg["max_age_hours"]:
        rejects.append("trop ancien")
    if (vol.get("h24") or 0) < cfg["min_volume_h24"]:
        rejects.append("volume faible")
    if liq and fdv / liq > cfg["max_fdv_to_liquidity"]:
        rejects.append("FDV/liquidité trop élevé")

    buys, sells = txh1.get("buys", 0), txh1.get("sells", 0)
    if sells == 0 and buys > 20:
        rejects.append("aucune vente (honeypot ?)")

    info = {}
    if use_rugcheck and not rejects and pair.get("chainId") == "solana":
        reasons, info = rugcheck(pair["baseToken"]["address"], cfg)
        if reasons is None:
            if cfg["require_rugcheck"]:
                rejects.append("RugCheck indisponible")
        else:
            rejects += [f"RugCheck: {x}" for x in reasons]
        time.sleep(0.3)  # rester poli avec l'API

    # --- score de dynamique (ce n'est PAS une prédiction)
    s = 0.0
    turnover = (vol.get("h1") or 0) / liq if liq else 0          # activité vs profondeur
    s += min(turnover, 2) / 2 * 25
    ratio = buys / (buys + sells) if buys + sells else 0.5        # pression acheteuse
    s += max(0, min((ratio - 0.5) / 0.2, 1)) * 20
    m5, h1, h6 = chg.get("m5") or 0, chg.get("h1") or 0, chg.get("h6") or 0
    s += 15 if 5 <= h1 <= 60 else (7 if 0 < h1 < 5 else 0)       # tendance, pas parabole
    s += 10 if h6 > 0 else 0
    s += min(liq / 200_000, 1) * 15                                # profondeur
    s += 15 if 6 <= age_h <= 72 else (8 if age_h > 72 else 3)
    if m5 > 25:                                                    # on ne chasse pas les pumps
        s -= 15
    score = round(max(0, min(s, 100)))

    return {
        "address": pair["baseToken"]["address"],
        "symbol": pair["baseToken"].get("symbol", "?"),
        "price": float(pair.get("priceUsd") or 0),
        "liq": liq, "fdv": fdv, "age_h": age_h,
        "h1": h1, "h24": chg.get("h24") or 0, "buy_ratio": ratio,
        "score": score, "rejects": rejects, "url": pair.get("url", ""),
        "sources": sorted(sources),
        "features": {
            "score": score, "liq": round(liq), "fdv_liq": round(fdv / liq, 2) if liq else 0,
            "age_h": round(age_h, 1), "turnover": round(turnover, 3),
            "buy_ratio": round(ratio, 3), "m5": m5, "h1": h1, "h6": h6,
            "boost": int("boost" in sources), **info,
        },
    }


def scan(cfg, demo=False):
    if demo:
        pairs, srcs = demo_pairs(), {}
    else:
        print("Récupération des candidats (DexScreener + GeckoTerminal)…")
        srcs = fetch_candidates(cfg["chain"])
        print(f"  {len(srcs)} tokens trouvés, analyse en cours…")
        pairs = fetch_pairs(cfg["chain"], srcs)
    res = [analyse(p, cfg, srcs.get(p["baseToken"]["address"], ("demo",)), not demo)
           for p in pairs]
    rej = {}
    for r in res:
        for x in r["rejects"]:
            k = x.split(" (")[0]
            rej[k] = rej.get(k, 0) + 1
    ok = sum(1 for r in res if not r["rejects"])
    print(f"  {ok} retenus / {len(res)} ; rejets : "
          + ", ".join(f"{k} ×{v}" for k, v in sorted(rej.items(), key=lambda x: -x[1])))
    return sorted(res, key=lambda r: (not r["rejects"], r["score"]), reverse=True)


def print_scan(results, show_rejected=False):
    ok = [r for r in results if not r["rejects"]]
    print(f"\n{'SYMB':<10}{'SCORE':>6}{'PRIX $':>14}{'LIQ $':>11}{'FDV $':>12}"
          f"{'ÂGE h':>7}{'1h %':>8}{'24h %':>8}{'ACHAT':>7}  SOURCES")
    for r in ok:
        print(f"{r['symbol'][:9]:<10}{r['score']:>6}{r['price']:>14.8g}{r['liq']:>11,.0f}"
              f"{r['fdv']:>12,.0f}{r['age_h']:>7.0f}{r['h1']:>8.1f}{r['h24']:>8.1f}"
              f"{r['buy_ratio']*100:>6.0f}%  {','.join(r['sources'])}")
    print(f"\n{len(ok)} tokens passent les filtres, {len(results)-len(ok)} rejetés.")
    if show_rejected:
        for r in results:
            if r["rejects"]:
                print(f"  ✗ {r['symbol'][:12]:<13} {' | '.join(r['rejects'])}")


# ---------------------------------------------------------------- paper trading
def strat_cfg(cfg, name):
    return dict(cfg, **STRATEGIES[name])


def cost_pct(usd, liq, cfg):
    """Frais + impact sur le prix : plus la mise est grosse face à la liquidité, plus ça coûte."""
    return min(cfg["base_fee_pct"] + cfg["impact_mult"] * 100 * usd / max(liq, 1), 90)


def new_pf(cfg):
    return {"cash": cfg["start_balance_usd"], "positions": {}, "realized": 0.0,
            "fees": 0.0, "wins": 0, "losses": 0, "history": []}


def load_all(cfg):
    """Un portefeuille simulé par stratégie."""
    data = {}
    if PORTFOLIO_FILE.exists():
        try:
            data = json.loads(PORTFOLIO_FILE.read_text())
        except ValueError:
            pass
    if "cash" in data:  # ancien format : un seul portefeuille
        data = {}
    out = {}
    for name in STRATEGIES:
        pf = new_pf(cfg)
        pf.update(data.get(name, {}))
        out[name] = pf
    return out


def save_all(pfs):
    tmp = PORTFOLIO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(pfs, indent=1))
    tmp.replace(PORTFOLIO_FILE)


def equity(pf, cfg=CONFIG):
    """Valeur si on revendait tout maintenant, frais et impact compris."""
    total = pf["cash"]
    for p in pf["positions"].values():
        val = p["qty"] * p["last"]
        total += val * (1 - cost_pct(val, p.get("liq", 0), cfg) / 100)
    return total


def set_phase(active, cycle=0, chain=""):
    """Écrit l'état des 6 agents pour le tableau de bord."""
    try:
        STATUS_FILE.write_text(json.dumps({
            "active": active, "cycle": cycle, "chain": chain, "ts": time.time(),
        }))
    except OSError:
        pass


def journal(strat, action, sym, price, usd, note=""):
    new = not JOURNAL_FILE.exists()
    with JOURNAL_FILE.open("a") as f:
        if new:
            f.write("date,strategie,action,symbole,prix,montant_usd,note\n")
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{strat},{action},{sym},"
                f"{price:.10g},{usd:.2f},{note}\n")
    set_phase("fills")
    print(f"  [{strat}] {action} {sym} @ {price:.8g}$ — {usd:.2f}$ {note}")


def record_trade(pos, cfg, reason):
    """Enregistre un trade fermé avec les mesures du token au moment de l'achat."""
    pnl = (pos["proceeds"] / pos["cost0"] - 1) * 100 if pos["cost0"] else 0
    row = {"ouverture": time.strftime("%Y-%m-%d %H:%M", time.localtime(pos["opened"])),
           "fermeture": time.strftime("%Y-%m-%d %H:%M"), "strategie": cfg["name"],
           "symbole": pos["symbol"], "sources": "+".join(pos.get("sources", [])),
           "sortie": reason, "duree_h": round((time.time() - pos["opened"]) / 3600, 2),
           "pnl_pct": round(pnl, 2)}
    row.update({k: pos.get("features", {}).get(k, "") for k in FEATURES})
    new = not TRADES_FILE.exists()
    with TRADES_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def sell(pf, addr, price, fraction, cfg, reason):
    pos = pf["positions"][addr]
    qty = pos["qty"] * fraction
    gross = qty * price
    c = cost_pct(gross, pos.get("liq", 0), cfg)
    proceeds = gross * (1 - c / 100)
    cost = pos["cost"] * fraction
    pf["fees"] += gross - proceeds
    pf["wins" if proceeds > cost else "losses"] += 1
    pf["cash"] += proceeds
    pf["realized"] += proceeds - cost
    pos["qty"] -= qty
    pos["cost"] -= cost
    pos["proceeds"] = pos.get("proceeds", 0) + proceeds
    journal(cfg["name"], "VENTE", pos["symbol"], price, proceeds,
            f"{reason} PnL {proceeds-cost:+.2f}$ coût {c:.1f}%")
    if fraction >= 0.999:
        record_trade(pos, cfg, reason)
        del pf["positions"][addr]


def manage_positions(pf, market, cfg):
    """market : adresse -> (prix, liquidité) ; absent = introuvable ce cycle."""
    for addr in list(pf["positions"]):
        pos = pf["positions"][addr]
        if addr not in market:
            pos["missing"] = pos.get("missing", 0) + 1
            if pos["missing"] >= cfg["missing_cycles_writeoff"]:
                pos["liq"] = 0  # pool disparue : on considère tout perdu
                sell(pf, addr, 0.0, 1.0, cfg, "token disparu (rug ?)")
            continue
        price, liq = market[addr]
        pos.update(missing=0, last=price, liq=liq, high=max(pos["high"], price))
        pnl = (price / pos["entry"] - 1) * 100
        held_h = (time.time() - pos["opened"]) / 3600
        ladder = cfg["tp_ladder"]
        # le prix n'est vu qu'à chaque cycle : en cas de chute brutale, la vente
        # se fait au prix observé, bien plus bas que le seuil (comme en vrai)
        if pnl <= cfg["stop_loss_pct"]:
            sell(pf, addr, price, 1.0, cfg, f"stop-loss ({pnl:+.0f}%)")
        elif pos["tp_idx"] < len(ladder) and pnl >= ladder[pos["tp_idx"]][0]:
            frac = ladder[pos["tp_idx"]][1]
            pos["tp_idx"] += 1
            sell(pf, addr, price, frac, cfg, f"palier +{ladder[pos['tp_idx']-1][0]}%")
        elif pos["tp_idx"] and price <= pos["high"] * (1 - cfg["trailing_stop_pct"] / 100):
            sell(pf, addr, price, 1.0, cfg, "trailing-stop")
        elif not pos["tp_idx"] and held_h >= cfg["max_hold_hours"]:
            sell(pf, addr, price, 1.0, cfg, f"durée max {cfg['max_hold_hours']}h")


def open_positions(pf, results, cfg, n_random=0):
    eligible = [r for r in results if not r["rejects"] and r["address"] not in pf["positions"]]
    if cfg.get("random"):
        # même rythme d'achat que la stratégie actuelle, mais tokens tirés au sort
        eligible = random.sample(eligible, min(n_random, len(eligible)))
    else:
        eligible = [r for r in eligible if r["score"] >= cfg["min_score_to_buy"]]
    for r in eligible:
        if len(pf["positions"]) >= cfg["max_open_positions"]:
            break
        usd = min(equity(pf, cfg) * cfg["position_size_pct"] / 100, pf["cash"])
        if usd < 1 or not r["price"]:
            break
        c = cost_pct(usd, r["liq"], cfg)
        entry = r["price"] * (1 + c / 100)
        pf["cash"] -= usd
        pf["fees"] += usd * c / (100 + c)
        pf["positions"][r["address"]] = {
            "symbol": r["symbol"], "entry": entry, "qty": usd / entry, "cost": usd,
            "cost0": usd, "proceeds": 0.0, "high": entry, "last": r["price"],
            "liq": r["liq"], "tp_idx": 0, "opened": time.time(), "missing": 0,
            "sources": r["sources"], "features": r["features"],
        }
        journal(cfg["name"], "ACHAT", r["symbol"], entry, usd,
                f"score {r['score']} coût {c:.1f}%")


def print_pf(pfs, cfg):
    start = cfg["start_balance_usd"]
    print(f"\n{'STRATÉGIE':<11}{'CAPITAL':>11}{'P&L %':>9}{'RÉALISÉ':>11}{'FRAIS':>9}"
          f"{'W/L':>9}{'POS.':>6}")
    for name, pf in pfs.items():
        eq = equity(pf, cfg)
        print(f"{name:<11}{eq:>10.2f}${(eq/start-1)*100:>+8.1f}%{pf['realized']:>+10.2f}$"
              f"{pf['fees']:>8.2f}${pf['wins']:>5}/{pf['losses']:<3}{len(pf['positions']):>6}")
    print("(argent fictif — valeur nette si tout était revendu maintenant)")


# ---------------------------------------------------------------- étude des trades
def study():
    """Compare les stratégies et regarde quelles mesures annonçaient les gains."""
    if not TRADES_FILE.exists():
        return {"n": 0, "strategies": {}, "features": []}
    with TRADES_FILE.open() as f:
        rows = list(csv.DictReader(f))
    out = {"n": len(rows), "strategies": {}, "features": []}
    for name in STRATEGIES:
        p = [float(r["pnl_pct"]) for r in rows if r["strategie"] == name]
        if p:
            out["strategies"][name] = {
                "n": len(p), "moyenne": round(statistics.mean(p), 2),
                "mediane": round(statistics.median(p), 2),
                "gagnants": round(sum(x > 0 for x in p) / len(p) * 100, 1)}
    # pour chaque mesure : les trades au-dessus de la médiane font-ils mieux ?
    for feat in FEATURES:
        pts = []
        for r in rows:
            try:
                pts.append((float(r[feat]), float(r["pnl_pct"])))
            except (ValueError, KeyError):
                pass
        if len(pts) < 10:
            continue
        med = statistics.median(x for x, _ in pts)
        lo = [p for x, p in pts if x <= med]
        hi = [p for x, p in pts if x > med]
        if lo and hi:
            out["features"].append({"mesure": feat, "seuil": round(med, 3),
                                    "bas_n": len(lo), "bas_moy": round(statistics.mean(lo), 2),
                                    "haut_n": len(hi), "haut_moy": round(statistics.mean(hi), 2)})
    return out


def print_study():
    s = study()
    print(f"\n{s['n']} trades fermés.")
    if s["n"] < 30:
        print("Moins de 30 trades : trop peu pour conclure quoi que ce soit.")
    print(f"\n{'STRATÉGIE':<11}{'TRADES':>8}{'MOY %':>9}{'MÉD %':>9}{'GAGNANTS':>10}")
    for n, v in s["strategies"].items():
        print(f"{n:<11}{v['n']:>8}{v['moyenne']:>+9.1f}{v['mediane']:>+9.1f}{v['gagnants']:>9.0f}%")
    if s["features"]:
        print(f"\n{'MESURE':<11}{'SEUIL':>10}{'≤ seuil (n, moy%)':>22}{'> seuil (n, moy%)':>22}")
        for f in s["features"]:
            print(f"{f['mesure']:<11}{f['seuil']:>10.3g}{f['bas_n']:>10} {f['bas_moy']:>+9.1f}%"
                  f"{f['haut_n']:>12} {f['haut_moy']:>+9.1f}%")
        print("\nUn écart n'est fiable qu'avec beaucoup de trades (des centaines).")


# ---------------------------------------------------------------- boucle
def acquire_lock():
    """Empêche deux bots de tourner en même temps."""
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime > 600:
            LOCK_FILE.unlink()  # verrou abandonné
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def git_sync():
    """Sur GitHub Actions : publie l'état dans le dépôt (lu par le tableau de bord)."""
    try:
        subprocess.run([sys.executable, str(DATA_DIR / "dashboard.py"), "--export", "docs"],
                       cwd=DATA_DIR, check=True, capture_output=True)
        files = [f.name for f in (PORTFOLIO_FILE, JOURNAL_FILE, TRADES_FILE, STATUS_FILE)
                 if f.exists()] + ["state.json", "bot.log"]
        files = [f for f in files if (DATA_DIR / f).exists()]
        sh = lambda *a: subprocess.run(a, cwd=DATA_DIR, capture_output=True, text=True)
        sh("cp", "docs/state.json", "state.json")
        sh("git", "add", *files)
        if sh("git", "diff", "--cached", "--quiet").returncode == 0:
            return
        sh("git", "commit", "-m", f"cycle {time.strftime('%Y-%m-%dT%H:%M')}")
        for _ in range(3):
            sh("git", "pull", "--rebase", "--autostash", "-X", "theirs")
            if sh("git", "push").returncode == 0:
                return
            time.sleep(5)
        print("  [!] push impossible", file=sys.stderr)
    except Exception as e:
        print(f"  [!] synchronisation : {e}", file=sys.stderr)


def bot(cfg, demo=False, cycles=None, minutes=None, sync_every=0):
    if not acquire_lock():
        print("Un bot tourne déjà, on passe.")
        return
    print("=== BOT EN MODE SIMULATION (argent fictif) — Ctrl+C pour arrêter ===")
    pfs = load_all(cfg)
    deadline = time.time() + minutes * 60 if minutes else None
    n = 0
    try:
        while (cycles is None or n < cycles) and (deadline is None or time.time() < deadline):
            t0 = time.time()
            LOCK_FILE.touch()
            n += 1
            print(f"\n--- cycle {n} — {time.strftime('%Y-%m-%d %H:%M:%S')} ---", flush=True)
            set_phase("scan", n, cfg["chain"])
            results = scan(cfg, demo)
            set_phase("vet", n, cfg["chain"])
            market = {r["address"]: (r["price"], r["liq"]) for r in results if r["price"]}
            held = {a: p for pf in pfs.values() for a, p in pf["positions"].items()}
            missing = [a for a in held if a not in market]
            if missing:  # tokens détenus absents du scan
                if demo:
                    for a in missing:
                        if random.random() > 0.03:
                            market[a] = (demo_price(held[a]["last"]), held[a].get("liq", 1e5))
                else:
                    for p in fetch_pairs(cfg["chain"], missing):
                        if p.get("priceUsd"):
                            market[p["baseToken"]["address"]] = (
                                float(p["priceUsd"]), (p.get("liquidity") or {}).get("usd") or 0)
            base = strat_cfg(cfg, "actuelle")
            n_random = sum(1 for r in results
                           if not r["rejects"] and r["score"] >= base["min_score_to_buy"])
            for name, pf in pfs.items():
                scfg = strat_cfg(cfg, name)
                set_phase("risk", n, cfg["chain"])
                manage_positions(pf, market, scfg)
                set_phase("size", n, cfg["chain"])
                open_positions(pf, results, scfg, n_random)
                pf["history"] = (pf["history"] + [[round(time.time()), round(equity(pf, scfg), 2)]])[-3000:]
            set_phase("book", n, cfg["chain"])
            save_all(pfs)
            set_phase(None, n, cfg["chain"])
            print_pf(pfs, cfg)
            sys.stdout.flush()
            if sync_every and n % sync_every == 0:
                git_sync()
            if not demo and (cycles is None or n < cycles):
                time.sleep(max(0, cfg["scan_interval_s"] - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        save_all(pfs)
        LOCK_FILE.unlink(missing_ok=True)
        if sync_every:
            git_sync()


# ---------------------------------------------------------------- mode démo
_DEMO = {}


def demo_price(p):
    # marche aléatoire à forte volatilité, biais négatif (réaliste pour les memes)
    return p * max(0.05, random.lognormvariate(-0.03, 0.25))


def demo_pairs():
    if not _DEMO:
        for i in range(25):
            addr = f"DEMO{i:02d}" + "x" * 36
            _DEMO[addr] = {
                "sym": random.choice(["PEPE", "DOGE", "CAT", "WIF", "BONK", "FROG", "MOON"]) + str(i),
                "price": 10 ** random.uniform(-7, -2),
                "liq": 10 ** random.uniform(3.5, 5.7),
                "age": random.uniform(0.2, 200),
            }
    out = []
    for addr, t in _DEMO.items():
        old = t["price"]
        t["price"] = demo_price(old)
        b, s = random.randint(5, 400), random.randint(0, 300)
        out.append({
            "chainId": "solana", "url": "", "priceUsd": str(t["price"]),
            "baseToken": {"address": addr, "symbol": t["sym"]},
            "liquidity": {"usd": t["liq"]}, "fdv": t["liq"] * random.uniform(3, 60),
            "volume": {"h1": t["liq"] * random.uniform(0, 2), "h24": t["liq"] * random.uniform(0.5, 15)},
            "priceChange": {"m5": random.uniform(-10, 35), "h1": (t["price"]/old-1)*100,
                            "h6": random.uniform(-50, 80), "h24": random.uniform(-80, 300)},
            "txns": {"h1": {"buys": b, "sells": s}},
            "pairCreatedAt": (time.time() - t["age"] * 3600) * 1000,
        })
    return out


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Scanner de meme coins + paper trading")
    ap.add_argument("cmd", choices=["scan", "bot", "portfolio", "analyse", "reset"])
    ap.add_argument("--demo", action="store_true", help="données inventées, sans internet")
    ap.add_argument("--rejected", action="store_true", help="affiche les tokens rejetés")
    ap.add_argument("--cycles", type=int, help="nombre de cycles du bot (défaut : infini)")
    ap.add_argument("--minutes", type=float, help="durée maximale du bot")
    ap.add_argument("--sync-every", type=int, default=0,
                    help="GitHub : publie l'état tous les N cycles")
    ap.add_argument("--chain", default=CONFIG["chain"], help="solana, base, ethereum, bsc…")
    a = ap.parse_args()
    cfg = dict(CONFIG, chain=a.chain)

    if a.cmd == "scan":
        print_scan(scan(cfg, a.demo), a.rejected)
    elif a.cmd == "bot":
        bot(cfg, a.demo, a.cycles, a.minutes, a.sync_every)
    elif a.cmd == "portfolio":
        print_pf(load_all(cfg), cfg)
    elif a.cmd == "analyse":
        print_study()
    elif a.cmd == "reset":
        for f in (PORTFOLIO_FILE, JOURNAL_FILE, TRADES_FILE, STATUS_FILE, LOCK_FILE):
            f.unlink(missing_ok=True)
        print("Portefeuilles simulés remis à zéro.")


if __name__ == "__main__":
    main()
