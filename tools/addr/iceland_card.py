"""What the Iceland card's REFER headers declare."""
import os

BOUNDS = (4160, 3520, -1680, -720)   # lat max, lat min, lon min, lon max, arc-minutes
# the card's own vintage, which the firmware checks: 14/11/17/01 on the 2014 card (ZE0), 17/09/01/01 on the 2017 card (ZE1, ME0)
VERSION = os.environ.get('CLARION_DATA_VERSION', '14/11/17/01')
