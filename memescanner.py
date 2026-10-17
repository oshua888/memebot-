#!/usr/bin/env python3
"""
MemeScanner — scanner de meme coins + simulateur d'achat (paper trading).

AVERTISSEMENT : aucun outil ne peut prédire quel token va "exploser".
Ce programme filtre les tokens les plus risqués et classe les autres selon
leur dynamique du moment. Il trade en ARGENT FICTIF uniquement.

Sources de données (publiques, sans clé API) :
  - DexScreener  : https://docs.dexscreener.com/api/reference
  - RugCheck     : https://api.rugcheck.xyz (Solana uniquement)

Usage :
  python3 memescanner.py scan                 # affiche le classement
  python3 memescanner.py bot                  # boucle : scan + achats/ventes simulés
  python3 memescanner.py portfolio            # état du portefeuille simulé
  python3 memescanner.py reset                # remet le portefeuille à zéro
  ajouter --demo pour tester avec des données inventées (sans internet)
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- paramètres
CONFIG = {
    "chain": "solana",
    "min_liquidity_usd": 25_000,     # en dessous : trop facile à manipuler
    "min_age_hours": 2,              # les tout premiers instants = zone de rug
    "max_age_hours": 24 * 14,
    "min_volume_h24": 50_000,
    "max_fdv_to_liquidity": 40,      # FDV énorme vs liquidité = prix fictif
    "max_rugcheck_score": 2000,      # score RugCheck brut (plus bas = mieux)
    "min_score_to_buy": 60,
    # paper trading
    "start_balance_usd": 1000.0,
    "position_size_pct": 2.0,        # % du capital par position
    "max_open_positions": 5,
    "stop_loss_pct": -30.0,
    "take_profit_pct": 100.0,        # on vend la moitié à +100 %
    "trailing_stop_pct": 25.0,       # puis on suit le plus haut à -25 %
    "fee_slippage_pct": 2.0,         # frais + slippage simulés par trade
    "scan_interval_s": 60,
}

# Trois stratégies simulées en parallèle, sur les mêmes tokens, pour comparer.
# Chacune remplace certains réglages de CONFIG.
STRATEGIES = {
    "prudente": {"min_score_to_buy": 70, "stop_loss_pct": -20.0,
                 "take_profit_pct": 50.0, "trailing_stop_pct": 15.0,
                 "position_size_pct": 1.0},
    "actuelle": {},
    "agressive": {"min_score_to_buy": 50, "stop_loss_pct": -40.0,
                  "take_profit_pct": 200.0, "trailing_stop_pct": 35.0,
                  "position_size_pct": 3.0},
}
for _name in STRATEGIES:
    STRATEGIES[_name]["name"] = _name

DATA_DIR = Path(__file__).resolve().parent
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
JOURNAL_FILE = DATA_DIR / "journal.csv"
STATUS_FILE = DATA_DIR / "status.json"
LOCK_FILE = DATA_DIR / ".bot.lock"

DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"


# ---------------------------------------------------------------- réseau
def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "memescanner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_candidates(chain):
    """Adresses des tokens récemment promus / mis en avant sur DexScreener."""
    addrs = []
    for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1",
                 "/token-profiles/latest/v1"):
        try:
            for item in get_json(DEX + path):
                if item.get("chainId") == chain and item.get("tokenAddress"):
                    addrs.append(item["tokenAddress"])
        except Exception as e:
            print(f"  [!] {path} : {e}", file=sys.stderr)
    return list(dict.fromkeys(addrs))  # dédoublonne en gardant l'ordre


def fetch_pairs(chain, addrs):
    """Meilleure paire (la plus liquide) pour chaque token."""
    best = {}
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


def fetch_price(chain, addr):
    pairs = fetch_pairs(chain, [addr])
    return float(pairs[0]["priceUsd"]) if pairs and pairs[0].get("priceUsd") else None


def rugcheck(addr):
    """Retourne (score, liste des risques 'danger') ou (None, []) si indisponible."""
    try:
        r = get_json(f"{RUGCHECK}/tokens/{addr}/report/summary")
        dangers = [x["name"] for x in r.get("risks", []) if x.get("level") == "danger"]
        return r.get("score"), dangers
    except Exception:
        return None, []


# ---------------------------------------------------------------- analyse
def analyse(pair, cfg, use_rugcheck=True):
    """Renvoie un dict avec score 0-100 et raisons de rejet éventuelles."""
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    fdv = pair.get("fdv") or pair.get("marketCap") or 0
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    txh1 = (pair.get("txns") or {}).get("h1") or {}
    created = pair.get("pairCreatedAt") or 0
    age_h = (time.time() * 1000 - created) / 3_600_000 if created else 0

    rejects = []
    if liq < cfg["min_liquidity_usd"]:
        rejects.append(f"liquidité {liq:,.0f}$")
    if age_h < cfg["min_age_hours"]:
        rejects.append(f"trop récent ({age_h:.1f}h)")
    if age_h > cfg["max_age_hours"]:
        rejects.append("trop ancien")
    if (vol.get("h24") or 0) < cfg["min_volume_h24"]:
        rejects.append("volume faible")
    if liq and fdv / liq > cfg["max_fdv_to_liquidity"]:
        rejects.append(f"FDV/liq {fdv/liq:.0f}x")

    buys, sells = txh1.get("buys", 0), txh1.get("sells", 0)
    if sells == 0 and buys > 20:
        rejects.append("aucune vente (honeypot ?)")

    rug_score, dangers = (None, [])
    if use_rugcheck and not rejects and pair.get("chainId") == "solana":
        rug_score, dangers = rugcheck(pair["baseToken"]["address"])
        if dangers:
            rejects.append("RugCheck: " + ", ".join(dangers[:2]))
        elif rug_score is not None and rug_score > cfg["max_rugcheck_score"]:
            rejects.append(f"RugCheck score {rug_score}")

    # --- score de dynamique (ce n'est PAS une prédiction)
    s = 0.0
    turnover = (vol.get("h1") or 0) / liq if liq else 0          # activité vs profondeur
    s += min(turnover, 2) / 2 * 25
    ratio = buys / (buys + sells) if buys + sells else 0.5        # pression acheteuse
    s += max(0, min((ratio - 0.5) / 0.2, 1)) * 20
    h1, h6 = chg.get("h1") or 0, chg.get("h6") or 0               # tendance, pas parabole
    s += 15 if 5 <= h1 <= 60 else (7 if 0 < h1 < 5 else 0)
    s += 10 if h6 > 0 else 0
    s += min(liq / 200_000, 1) * 15                                # profondeur
    s += 15 if 6 <= age_h <= 72 else (8 if age_h > 72 else 3)
    if (chg.get("m5") or 0) > 25:                                  # on ne chasse pas les pumps
        s -= 15

    return {
        "address": pair["baseToken"]["address"],
        "symbol": pair["baseToken"].get("symbol", "?"),
        "price": float(pair.get("priceUsd") or 0),
        "liq": liq, "fdv": fdv, "age_h": age_h,
        "h1": h1, "h24": chg.get("h24") or 0, "buy_ratio": ratio,
        "score": round(max(0, min(s, 100))),
        "rejects": rejects, "url": pair.get("url", ""),
    }


def scan(cfg, demo=False):
    if demo:
        pairs = demo_pairs()
    else:
        print("Récupération des candidats sur DexScreener…")
        addrs = fetch_candidates(cfg["chain"])
        print(f"  {len(addrs)} tokens trouvés, analyse en cours…")
        pairs = fetch_pairs(cfg["chain"], addrs)
    res = [analyse(p, cfg, use_rugcheck=not demo) for p in pairs]
    return sorted(res, key=lambda r: (not r["rejects"], r["score"]), reverse=True)


def print_scan(results, show_rejected=False):
    ok = [r for r in results if not r["rejects"]]
    print(f"\n{'SYMB':<10}{'SCORE':>6}{'PRIX $':>14}{'LIQ $':>11}{'FDV $':>12}"
          f"{'ÂGE h':>7}{'1h %':>8}{'24h %':>8}{'ACHAT':>7}")
    for r in ok:
        print(f"{r['symbol'][:9]:<10}{r['score']:>6}{r['price']:>14.8g}{r['liq']:>11,.0f}"
              f"{r['fdv']:>12,.0f}{r['age_h']:>7.0f}{r['h1']:>8.1f}{r['h24']:>8.1f}"
              f"{r['buy_ratio']*100:>6.0f}%")
    print(f"\n{len(ok)} tokens passent les filtres, {len(results)-len(ok)} rejetés.")
    if show_rejected:
        for r in results:
            if r["rejects"]:
                print(f"  ✗ {r['symbol'][:12]:<13} {' | '.join(r['rejects'])}")


# ---------------------------------------------------------------- paper trading
def strat_cfg(cfg, name):
    return dict(cfg, **STRATEGIES[name])


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


def equity(pf):
    return pf["cash"] + sum(p["qty"] * p["last"] for p in pf["positions"].values())


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


def sell(pf, addr, price, fraction, cfg, reason):
    pos = pf["positions"][addr]
    qty = pos["qty"] * fraction
    proceeds = qty * price * (1 - cfg["fee_slippage_pct"] / 100)
    cost = pos["cost"] * fraction
    pf["fees"] += qty * price * cfg["fee_slippage_pct"] / 100
    pf["wins" if proceeds > cost else "losses"] += 1
    pf["cash"] += proceeds
    pf["realized"] += proceeds - cost
    pos["qty"] -= qty
    pos["cost"] -= cost
    journal(cfg["name"], "VENTE", pos["symbol"], price, proceeds,
            f"{reason} PnL {proceeds-cost:+.2f}$")
    if fraction >= 0.999:
        del pf["positions"][addr]


def manage_positions(pf, prices, cfg):
    for addr in list(pf["positions"]):
        price = prices.get(addr)
        if not price:
            continue
        pos = pf["positions"][addr]
        pos["last"] = price
        pos["high"] = max(pos["high"], price)
        pnl = (price / pos["entry"] - 1) * 100
        if pnl <= cfg["stop_loss_pct"]:
            sell(pf, addr, price, 1.0, cfg, "stop-loss")
        elif not pos["tp_done"] and pnl >= cfg["take_profit_pct"]:
            sell(pf, addr, price, 0.5, cfg, "take-profit 50%")
            pos["tp_done"] = True
        elif pos["tp_done"] and price <= pos["high"] * (1 - cfg["trailing_stop_pct"] / 100):
            sell(pf, addr, price, 1.0, cfg, "trailing-stop")


def open_positions(pf, results, cfg):
    for r in results:
        if len(pf["positions"]) >= cfg["max_open_positions"]:
            break
        if r["rejects"] or r["score"] < cfg["min_score_to_buy"] or r["address"] in pf["positions"]:
            continue
        usd = min(equity(pf) * cfg["position_size_pct"] / 100, pf["cash"])
        if usd < 1:
            break
        entry = r["price"] * (1 + cfg["fee_slippage_pct"] / 100)
        pf["cash"] -= usd
        pf["fees"] += usd * cfg["fee_slippage_pct"] / 100
        pf["positions"][r["address"]] = {
            "symbol": r["symbol"], "entry": entry, "qty": usd / entry, "cost": usd,
            "high": entry, "last": r["price"], "tp_done": False, "opened": time.time(),
        }
        journal(cfg["name"], "ACHAT", r["symbol"], entry, usd, f"score {r['score']}")


def print_pf(pfs, cfg):
    start = cfg["start_balance_usd"]
    print(f"\n{'STRATÉGIE':<11}{'CAPITAL':>11}{'P&L %':>9}{'RÉALISÉ':>11}{'FRAIS':>9}"
          f"{'W/L':>9}{'POS.':>6}")
    for name, pf in pfs.items():
        eq = equity(pf)
        print(f"{name:<11}{eq:>10.2f}${(eq/start-1)*100:>+8.1f}%{pf['realized']:>+10.2f}$"
              f"{pf['fees']:>8.2f}${pf['wins']:>5}/{pf['losses']:<3}{len(pf['positions']):>6}")
    print("(argent fictif)")


def acquire_lock():
    """Empêche deux cycles de tourner en même temps (lancement chaque minute)."""
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime > 600:
            LOCK_FILE.unlink()  # verrou abandonné
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def bot(cfg, demo=False, cycles=None):
    if not acquire_lock():
        print("Un cycle est déjà en cours, on passe.")
        return
    print("=== BOT EN MODE SIMULATION (argent fictif) — Ctrl+C pour arrêter ===")
    pfs = load_all(cfg)
    n = 0
    try:
        while cycles is None or n < cycles:
            n += 1
            print(f"\n--- cycle {n} — {time.strftime('%H:%M:%S')} ---")
            set_phase("scan", n, cfg["chain"])
            results = scan(cfg, demo)
            set_phase("vet", n, cfg["chain"])
            prices = {r["address"]: r["price"] for r in results}
            held = {a: p["last"] for pf in pfs.values() for a, p in pf["positions"].items()}
            for addr, last in held.items():  # tokens détenus absents du scan
                if addr not in prices:
                    p = demo_price(last) if demo else fetch_price(cfg["chain"], addr)
                    if p:
                        prices[addr] = p
            for name, pf in pfs.items():
                scfg = strat_cfg(cfg, name)
                set_phase("risk", n, cfg["chain"])
                manage_positions(pf, prices, scfg)
                set_phase("size", n, cfg["chain"])
                open_positions(pf, results, scfg)
                pf["history"] = (pf["history"] + [[round(time.time()), round(equity(pf), 2)]])[-3000:]
            set_phase("book", n, cfg["chain"])
            save_all(pfs)
            set_phase(None, n, cfg["chain"])
            print_pf(pfs, cfg)
            if cycles is None or n < cycles:
                time.sleep(0 if demo else cfg["scan_interval_s"])
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        save_all(pfs)
        LOCK_FILE.unlink(missing_ok=True)


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
    ap.add_argument("cmd", choices=["scan", "bot", "portfolio", "reset"])
    ap.add_argument("--demo", action="store_true", help="données inventées, sans internet")
    ap.add_argument("--rejected", action="store_true", help="affiche les tokens rejetés")
    ap.add_argument("--cycles", type=int, help="nombre de cycles du bot (défaut : infini)")
    ap.add_argument("--chain", default=CONFIG["chain"], help="solana, base, ethereum, bsc…")
    a = ap.parse_args()
    cfg = dict(CONFIG, chain=a.chain)

    if a.cmd == "scan":
        print_scan(scan(cfg, a.demo), a.rejected)
    elif a.cmd == "bot":
        bot(cfg, a.demo, a.cycles)
    elif a.cmd == "portfolio":
        print_pf(load_all(cfg), cfg)
    elif a.cmd == "reset":
        for f in (PORTFOLIO_FILE, JOURNAL_FILE, STATUS_FILE, LOCK_FILE):
            f.unlink(missing_ok=True)
        print("Portefeuille simulé remis à zéro.")


if __name__ == "__main__":
    main()
