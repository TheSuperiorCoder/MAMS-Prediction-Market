import os

# Promotions (like the Black Friday bonus) pay out on real calendar dates. Keep them off so the suite gives the
# same balances whatever day it runs; tests/test_promo.py turns them on where it needs them.
os.environ["MAMS_PROMOS"] = "0"
