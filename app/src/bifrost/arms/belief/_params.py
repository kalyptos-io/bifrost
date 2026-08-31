"""shared scoring params for the belief branches - loaded once from the calibration artifact."""

from bifrost.db.aux import load_score_params

PARAMS = load_score_params()
