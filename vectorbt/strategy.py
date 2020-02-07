import pandas as pd
import numpy as np
from numba import njit
from numba.types import UniTuple, DictType, f8, i8, b1
from numba.typed import Dict
from vectorbt.timeseries import ewma_nb, rolling_mean_nb, rolling_std_nb, diff_nb, \
set_by_mask_nb, fillna_nb, prepend_nb, rolling_max_nb, pct_change_nb, ffill_nb

from vectorbt.utils.decorators import *
from vectorbt.signals import Signals, generate_exits_nb
from vectorbt.timeseries import TimeSeries

# ############# MovingAverage ############# #

float_2d_array = f8[:, :]


@njit(UniTuple(f8[:, :], 2)(f8[:, :], i8[:], i8[:], b1, b1, b1), cache=True)
def ma_nb(ts, fast_windows, slow_windows, ewm, adjust, min_periods):
    """For each fast and slow window, calculate the corresponding SMA/EMA."""
    # Cache moving averages to effectively reduce the number of operations
    unique_windows = np.unique(np.concatenate((fast_windows, slow_windows)))
    cache_d = Dict.empty(
        key_type=i8,
        value_type=float_2d_array,
    )
    for i in range(unique_windows.shape[0]):
        if ewm:
            ma = ewma_nb(ts, unique_windows[i], adjust)
        else:
            ma = rolling_mean_nb(ts, unique_windows[i])
        if min_periods:
            ma[:unique_windows[i], :] = np.nan
        cache_d[unique_windows[i]] = ma
    # Concatenate moving averages out of cache and return
    fast_mas = np.empty((ts.shape[0], ts.shape[1] * fast_windows.shape[0]), dtype=f8)
    slow_mas = np.empty((ts.shape[0], ts.shape[1] * fast_windows.shape[0]), dtype=f8)
    for i in range(fast_windows.shape[0]):
        fast_mas[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = cache_d[fast_windows[i]]
        slow_mas[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = cache_d[slow_windows[i]]
    return fast_mas, slow_mas


class MovingAverage():
    """The SMA is a technical indicator for determining if an asset price 
    will continue or reverse a bull or bear trend. The SMA is calculated as 
    the arithmetic average of an asset's price over some period.

    The EMA is a moving average that places a greater weight and 
    significance on the most recent data points."""

    @has_type('ts', TimeSeries)
    @to_2d('ts')
    @to_1d('fast_windows')
    @to_1d('slow_windows')
    @broadcast('fast_windows', 'slow_windows')
    @has_dtype('fast_windows', np.int64)
    @has_dtype('slow_windows', np.int64)
    def __init__(self, ts, fast_windows, slow_windows, ewm=False, adjust=False, min_periods=True):
        # fast_windows and slow_windows can be either np.ndarray or single number
        fast_mas, slow_mas = ma_nb(ts, fast_windows, slow_windows, ewm, adjust, min_periods)
        self.fast_mas = TimeSeries(fast_mas)
        self.slow_mas = TimeSeries(slow_mas)

    def generate_entries(self):
        return Signals(self.fast_mas > self.slow_mas)

    def generate_exits(self):
        return Signals(self.fast_mas < self.slow_mas)

# ############# BollingerBands ############# #


tuple_of_f2d_arrays = UniTuple(float_2d_array, 2)


@njit(UniTuple(f8[:, :], 3)(f8[:, :], i8[:], i8[:], b1), cache=True)
def bb_nb(ts, ns, ks, min_periods):
    """For each N and K, calculate the corresponding upper, middle and lower BB bands."""
    # Cache moving averages to effectively reduce the number of operations
    cache_d = Dict.empty(
        key_type=i8,
        value_type=tuple_of_f2d_arrays,
    )
    for i in range(ns.shape[0]):
        ma = rolling_mean_nb(ts, ns[i])
        mstd = rolling_std_nb(ts, ns[i])
        if min_periods:
            ma[:ns[i], :] = np.nan
            mstd[:ns[i], :] = np.nan
        cache_d[ns[i]] = ma, mstd
    # Calculate lower, middle and upper bands
    upper_bands = np.empty((ts.shape[0], ts.shape[1] * ns.shape[0]), dtype=f8)
    middle_bands = np.empty((ts.shape[0], ts.shape[1] * ns.shape[0]), dtype=f8)
    lower_bands = np.empty((ts.shape[0], ts.shape[1] * ns.shape[0]), dtype=f8)
    for i in range(ns.shape[0]):
        ma, mstd = cache_d[ns[i]]
        upper_bands[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = ma + ks[i] * mstd  # (MA + Kσ)
        middle_bands[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = ma  # MA
        lower_bands[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = ma - ks[i] * mstd  # (MA - Kσ)
    return upper_bands, middle_bands, lower_bands


class BollingerBands():
    """Bollinger Bands® are volatility bands placed above and below a moving average."""

    @has_type('ts', TimeSeries)
    @to_2d('ts')
    @to_1d('windows')
    @to_1d('std_ns')
    @broadcast('windows', 'std_ns')
    @has_dtype('windows', np.int64)
    @has_dtype('std_ns', np.int64)
    def __init__(self, ts, windows, std_ns, min_periods=True):
        # windows and std_ns can be either np.ndarray or single number
        self.ts = np.tile(ts, (1, windows.shape[0]))
        upper_bands, middle_bands, lower_bands = bb_nb(ts, windows, std_ns, min_periods)
        self.upper_bands = TimeSeries(upper_bands)
        self.middle_bands = TimeSeries(middle_bands)
        self.lower_bands = TimeSeries(lower_bands)

    @property
    def percent_b(self):
        """Shows where price is in relation to the bands.
        %b equals 1 at the upper band and 0 at the lower band."""
        return TimeSeries((self.ts - self.lower_bands) / (self.upper_bands - self.lower_bands))

    @property
    def bandwidth(self):
        """Bandwidth tells how wide the Bollinger Bands are on a normalized basis."""
        return TimeSeries((self.upper_bands - self.lower_bands) / self.middle_bands)

    def generate_entries(self):
        return Signals(self.ts >= self.upper_bands)

    def generate_exits(self):
        return Signals(self.ts <= self.lower_bands)


# ############# RSI ############# #

@njit(f8[:, :](f8[:, :], i8[:], b1, b1, b1), cache=True)
def rsi_nb(ts, windows, ewm, adjust, min_periods):
    """For each window, calculate the RSI."""
    delta = diff_nb(ts)[1:, :]  # otherwise ewma will be all NaN
    up, down = delta.copy(), delta.copy()
    up = set_by_mask_nb(up, up < 0, 0)
    down = np.abs(set_by_mask_nb(down, down > 0, 0))
    # Cache moving averages to effectively reduce the number of operations
    unique_windows = np.unique(windows)
    cache_d = Dict.empty(
        key_type=i8,
        value_type=tuple_of_f2d_arrays,
    )
    for i in range(unique_windows.shape[0]):
        if ewm:
            roll_up = ewma_nb(up, unique_windows[i], adjust)
            roll_down = ewma_nb(down, unique_windows[i], adjust)
        else:
            roll_up = rolling_mean_nb(up, unique_windows[i])
            roll_down = rolling_mean_nb(down, unique_windows[i])
        roll_up = prepend_nb(roll_up, 1, np.nan)  # bring to old shape
        roll_down = prepend_nb(roll_down, 1, np.nan)
        if min_periods:
            roll_up[:unique_windows[i], :] = np.nan
            roll_down[:unique_windows[i], :] = np.nan
        cache_d[unique_windows[i]] = roll_up, roll_down
    # Calculate RSI
    rsi = np.empty((ts.shape[0], ts.shape[1] * windows.shape[0]), dtype=f8)
    for i in range(windows.shape[0]):
        roll_up, roll_down = cache_d[windows[i]]
        rsi[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = 100 - 100 / (1 + roll_up / roll_down)
    return rsi


class RSI():
    """The relative strength index (RSI) is a momentum indicator that 
    measures the magnitude of recent price changes to evaluate overbought 
    or oversold conditions in the price of a stock or other asset."""

    @has_type('ts', TimeSeries)
    @to_2d('ts')
    @to_1d('windows')
    @has_dtype('windows', np.int64)
    def __init__(self, ts, windows, ewm=False, adjust=False, min_periods=True):
        self.rsi = TimeSeries(rsi_nb(ts, windows, ewm, adjust, min_periods))

    @to_2d('lower_bound')
    @broadcast_to('lower_bound', 'self.rsi')
    def generate_entries(self, lower_bound):
        return Signals(self.rsi < lower_bound)

    @to_2d('upper_bound')
    @broadcast_to('upper_bound', 'self.rsi')
    def generate_exits(self, upper_bound):
        return Signals(self.rsi > upper_bound)


# ############# Risk minimization ############# #

@njit(b1[:](b1[:, :], i8, i8, f8[:, :], f8[:, :], b1), cache=True)
def stoploss_exit_mask_nb(entries, col_idx, entry_idx, ts, stop, is_relative):
    """Index of the first event below the stop."""
    ts = ts[:, col_idx]
    # Stop is defined at the entry point
    stop = stop[entry_idx, col_idx]
    if is_relative:
        stop = (1 - stop) * ts[entry_idx]
    return ts < stop


@njit(b1[:, :](f8[:, :], b1[:, :], f8[:, :, :], b1, b1), cache=True)
def stoploss_exits_nb(ts, entries, stops, is_relative, only_first):
    """Calculate exit signals based on stop loss strategy.
    
    An approach here significantly differs from the approach with rolling windows.
    If user wants to try out different rolling windows, he can pass them as a 1d array.
    Here, user must be able to try different stops not only for the `ts` itself,
    but also for each element in `ts`, since stops may vary with time.
    This requires the variable `stops` to be a 3d array (cube) out of 2d matrices of form of `ts`.
    For example, if you want to try stops 0.1 and 0.2, both must have the shape of `ts`,
    wrapped into an array, thus forming a cube (2, ts.shape[0], ts.shape[1])"""

    exits = np.empty((ts.shape[0], ts.shape[1] * stops.shape[0]), dtype=b1)
    for i in range(stops.shape[0]):
        i_exits = generate_exits_nb(entries, stoploss_exit_mask_nb, only_first, ts, stops[i, :, :], is_relative)
        exits[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = i_exits
    return exits


@has_type('ts', TimeSeries)
@has_type('entries', Signals)
@to_2d('ts')
@to_2d('entries')
@broadcast('ts', 'entries')
@broadcast_to_cube_of('stops', 'ts')
# stops can be either a number, an array of numbers, or an array of matrices each of ts shape
def generate_stoploss_exits(ts, entries, stops, is_relative=True, only_first=True):
    """A stop-loss is designed to limit an investor's loss on a security position. 
    Setting a stop-loss order for 10% below the price at which you bought the stock 
    will limit your loss to 10%."""

    return Signals(stoploss_exits_nb(ts, entries, stops, is_relative, only_first))


@njit(b1[:](b1[:, :], i8, i8, f8[:, :], f8[:, :], b1), cache=True)
def tstop_exit_mask_nb(entries, col_idx, entry_idx, ts, stop, is_relative):
    """Index of the first event below the trailing stop."""
    ts = np.expand_dims(ts[:, col_idx], axis=1) # most nb function perform on 2d data only
    stop = np.expand_dims(stop[:, col_idx], axis=1)
    peak = np.full(ts.shape, np.nan)
    # Propagate the maximum value from the entry using expanding max
    peak[entry_idx:, :] = rolling_max_nb(ts[entry_idx:, :], None)
    if np.min(stop) != np.max(stop):
        # Propagate the stop value of the last max
        raising_idxs = np.flatnonzero(pct_change_nb(peak))
        stop_temp = np.full(ts.shape, -np.inf)
        stop_temp[raising_idxs, :] = stop[raising_idxs, :]
        stop = fillna_nb(ffill_nb(stop_temp), -np.inf)
    if is_relative:
        stop = (1 - stop) * peak
    return (ts < stop)[:, 0]


@njit(b1[:, :](f8[:, :], b1[:, :], f8[:, :, :], b1, b1), cache=True)
def tstop_exits_nb(ts, entries, stops, is_relative, only_first):
    """Calculate exit signals based on trailing stop strategy."""
    exits = np.empty((ts.shape[0], ts.shape[1] * stops.shape[0]), dtype=b1)
    for i in range(stops.shape[0]):
        i_exits = generate_exits_nb(entries, tstop_exit_mask_nb, only_first, ts, stops[i, :, :], is_relative)
        exits[:, i*ts.shape[1]:(i+1)*ts.shape[1]] = i_exits
    return exits


@has_type('ts', TimeSeries)
@has_type('entries', Signals)
@to_2d('ts')
@to_2d('entries')
@broadcast('ts', 'entries')
@broadcast_to_cube_of('stops', 'ts')
# stops can be either a number, an array of numbers, or an array of matrices each of ts shape
def generate_tstop_exits(ts, entries, stops, is_relative=True, only_first=True):
    """A Trailing Stop order is a stop order that can be set at a defined percentage 
    or amount away from the current market price. The main difference between a regular 
    stop loss and a trailing stop is that the trailing stop moves as the price moves."""

    return Signals(tstop_exits_nb(ts, entries, stops, is_relative, only_first))
