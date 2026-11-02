"""Price sources (epics Live-Kurse und Kursdatenspeicher, Historische Markt- und Ereignisdaten): each returns the prices
of Wertpapiere in the shapes of base.py. Alpaca (alpaca.py): every US stock and ETF, its daily history and corporate
actions, live prices from IEX; SIX (six.py): Swiss live prices, delayed; EODHD (eodhd.py): others, delayed, with a paid
key - the admin's decisions of 2026-10-31: free, delayed is fine, never throttled.
"""
