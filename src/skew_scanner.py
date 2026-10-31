import numpy as np
from scipy.signal import savgol_filter
from scipy.stats import linregress
import math
import os
import time


# TODO think about using wider range in x and y direction, currently for 70x70mm plate, the range is only 34mm
#   for 100x100 plate it is axb mm ... to be measured.
#   This is to avoid scanning to near to the edge
#   The accuracy raise linearly with this range -> with a 68x68 range, the accuracy would be doubled (failure halfed)


# TODO replace calls to "private" cartographer APIs by suitable wrapper

# if DEBUG=1, the scan results are written to: ~/printer_data/gcodes/measure_skew_scan_results.txt


# Version 20
# Copyright Uwe Damm
#  No license restrictions. Use it however you like.
#  use the concept
#  use the script
#  use the results
#  modify and store in different place
# But not:
#  blame me for any trouble
# Bug reports, spell checking, and other feedback are always welcome.

# how to use in short:
# Note: To use this script, you need a 50x50 mm to 150x150 mm
#   aluminium plate or block.
# - Switch on the Voron.
# - CHOME  (G28)
# - QGL
# - G1 X175 Y175 Z60  (above aluminium plate, set your own z-homing position here)
# - place the aluminium plate below cartographer (carto approx middle of the plate), nearly orthogonal with the build plate
# CHOME (home on the top of the plate), Z-homing xy coordinates MUST be approx middle of the plate)
# MEASURE_SKEW XCENTER=175.0 YCENTER=175.0 ZHEIGHT=2.0 SCANSPEED=20 MOVESPEED=200 SAVEDIST=25.0 NMEAS=9
#   add EDGESCAN=1 to scan slowly only around the plate edges (faster, see README)
# Rotate the plate by 90° around the Z-axis.
# Repeat the previous two steps four times.

# How to install this script into klipper:
# Add to printer.cfg:
#  [skew_scanner]
#
# ----------------------------------------
# login to RPI, which controls the printer (E.g. via ssh)
# $ rm ~/klipper/klippy/extras/skew_scanner.py
# $ vim ~/klipper/klippy/extras/skew_scanner.py
#   Note: you can use whatever texteditor you want or simply copy/paste the file
# paste content of this file into skew_scanner.py
# restart klipper
# $ sudo systemctl restart klipper


class SkewScanner:
    POLYFIT_EPSILON = 1e-12

    # Minimum of 4 points required.
    # More points improve polynomial-fit robustness.
    MIN_INFLECTION_POINTS = 10

    TOOLHEAD_DWELL_AFTER_SCAN_S=0.251

    # Indices used for pos[].
    # The values 0, 1 and 2 must not be changed.
    DIRX = 0
    DIRY = 1
    DIRZ = 2

    CORNER_LOCATIONS = ["SW","SE","NE","NW"]

    # EDGESCAN=1: scan slowly only in a window around each plate edge and
    # fast in between. The window size is derived from a calibration line
    # (first line of each sweep, scanned slowly over the full width):
    #   off-plate side = EDGE_OFF_FACTOR * distance edge -> 5% signal level
    #   plate side     = EDGE_ON_FACTOR  * distance edge -> 95% signal level
    # EDGEOFF/EDGEON override the automatic values.
    EDGE_OFF_FACTOR = 1.0
    EDGE_ON_FACTOR = 2.0
    EDGE_WIN_MIN = 6.0         # mm, lower clamp of an auto-sized window side
    EDGE_WIN_MAX = 40.0        # mm, upper clamp of an auto-sized window side
    EDGE_PAD = 2.0             # mm, slow section is padded by this on both ends,
                               #   so the window can be re-centred on the found edge
    EDGE_MIN_FAST = 10.0       # mm, shorter fast sections are scanned slowly instead
    EDGE_MIN_CONTRAST = 0.7    # window signal range must be >= this * calibration range
    EDGE_CENTER_TOL = 0.005    # mm, window re-centring has converged
    EDGE_CENTER_ITER = 5
    EDGE_CENTER_ACCEPT = 0.05  # mm, accept a window still moving less than this
    COARSESPEED_EDGESCAN = 100.0  # mm/s, default speed of the two coarse scans with EDGESCAN=1

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        self.gcmd = None

# TAKE care, if the printing/writing to file takes "too long", the scanner will through error
#    Scanner sensor not receiving data. Transition to shutdown state:
#   This failure also comes, when 3D printing takes place, after the calibration is done (reboot before!)
# enable writing scans to txt-file
        self.DEBUGSCANS2TXT = False         # Better keep False here, see above
# enable additional printouts to the klippy.log and console screen
        self.DEBUGPRINT = False


        # if this class is loaded (during klipper start),
        #  it initializes for the first measurement (MEASURE_SKEW)
        # Each time MEASURE_SKEW is called, the numbered corner
        # must point towards the SW direction.
        self.meascnt = 1
        self.probeSW_res = [0.0] * 4
        self.probeNW_res = [0.0] * 4
        self.probeNE_res = [0.0] * 4
        self.probeSE_res = [0.0] * 4
        self.phi_plate_xy = None
        self.phi_xy = None


        self.currx = None
        self.curry = None
        self.currz = None

        # middle of the plate (should be the coordinates, where z-homing is done, approx middle of the aluminium plate)
        self.xcenter = None
        self.ycenter = None
        self.z_height = None   # height of the nozzle above the plate, while scanning

        self.movespeed = None  # mm/s while moving without scanning (maybe increased, should not matter)
        self.scanspeed = None   # mm/s while scanning (20mm/s seems to be accurate)

        self.n = 0    # number of the currently measured scan

        # number of scans in x and y direction (9 seems to be accurate)
        # must be >=2 (else lin-reg will fail!)
        self.nmeas = None

        # allscans is filled with all scans results, to be able to write to output file (for debugging)
        self.allscans = []

        # Register the custom G-Code command
        self.gcode.register_command('MEASURE_SKEW', self.create_MEASURE_SKEW,
                                    desc="Scan an aluminium plate in x and y direction to measure the xy-skew")

    def log(self, msg):
        if self.gcmd is not None:
            self.gcmd.respond_info(f"MEASURE_SKEW: {msg}\n")


    def restart_all(self):
        self.n = 0
        self.meascnt = 1
        self.probeSW_res = [0.0] * 4
        self.probeNW_res = [0.0] * 4
        self.probeNE_res = [0.0] * 4
        self.probeSE_res = [0.0] * 4
        self.allscans = []
        self.phi_plate_xy = None
        self.phi_xy = None

        self.log(f"restart_all: Place (1) to SW, to start new skew measurement")




    def meas2geo(self,phi1,phi2):
        # return:
        #   phi_plate = sum phi1,phi2
        #   phi_xy = diff phi1,phi2
        phi_plate = .5 * (phi2+phi1)
        phi_xy    = 90.0 + 0.5 * (phi2-phi1)
        return phi_plate,phi_xy


    def calc_geometry_xy(self):
        # inputs:
        # probeSW_res[4], etc. (measured angles for the 4 plate-corners, 4 measurements with rotated plate
        # outputs:
        #   self.phi_xy[4][4]    [id][dir]. id is the identifier for the edge of the plate. dir is the direction in which the edge was measured
        #   self.phi_plate_xy[4][4]
        #   m_phi_plate_xy[4]   angles of the 4 plate corners, as result of all the measurements

        self.phi_plate_xy = [[0] * 4 for _ in range(4)]
        self.phi_xy = [[0] * 4 for _ in range(4)]

        for edgeId in range(4):
            # calculate (phi1+phi2)/2 as self.phi_plate_xy (plategeometry)
            # calculate difference as self.phi_xy (xyskew)
            [self.phi_plate_xy[edgeId][0],self.phi_xy[edgeId][0]] = self.meas2geo(self.probeSW_res[edgeId],self.probeNW_res[(edgeId+3)%4])
            [self.phi_plate_xy[edgeId][1],self.phi_xy[edgeId][1]] = self.meas2geo(self.probeNE_res[(edgeId+2)%4],self.probeNW_res[(edgeId+3)%4])
            [self.phi_plate_xy[edgeId][2],self.phi_xy[edgeId][2]] = self.meas2geo(self.probeNE_res[(edgeId+2)%4],self.probeSE_res[(edgeId+1)%4])
            [self.phi_plate_xy[edgeId][3],self.phi_xy[edgeId][3]] = self.meas2geo(self.probeSW_res[edgeId],self.probeSE_res[(edgeId+1)%4])


    def print_skew(self,phi_xy):
        # 1. Klipper benötigt 3 Distanzen. Wir nehmen ein perfektes 100-mm-Quadrat als Basis (AD = 100)
        ad = 100.0

        # 2. Berechnung der Diagonalen AC und BD mittels Kosinussatz basierend auf deinem Winkel

        # Beim ersten Ansatz mit copilot waren ac und bd vertauscht.
        # E.g. set_skew in printer.cfg and check, how the measurement reacts on it.
        # AC liegt gegenüber dem Winkel phi_xy
        ac = math.sqrt(ad**2 + ad**2 - 2 * ad * ad * math.cos(math.radians(180.0 - phi_xy)))
        # BD liegt gegenüber dem Supplementärwinkel (180 - phi_xy)
        bd = math.sqrt(ad**2 + ad**2 - 2 * ad * ad * math.cos(math.radians(phi_xy)))


        # 3. Deine bestehenden Berechnungen
        dev_mm = 1000 * math.sin(math.radians(90.0 - phi_xy))
        xy_skew_factor = math.tan(math.radians(90.0 - phi_xy))

        # Log-Ausgabe erweitern/beibehalten
        self.log(f"dev_mm={dev_mm:.5f}, xy_skew_factor={xy_skew_factor:.5f}")

        # Der gewünschte f-String für das Klipper-Kommando (auf 2 Nachkommastellen gerundet)
        klipper_cmd = f"SET_SKEW XY={ac:.2f},{bd:.2f},{ad:.1f}"
        self.log(klipper_cmd)



    def calc_skew(self):
        #use probeSW_res[4], etc. as input for the algorithm
        self.calc_geometry_xy()

        # print out some measurement results (usually not relevant -> commented out)
        if self.DEBUGPRINT:
            for edgeId in range(4):
                self.log(f"probeSW_res[{edgeId}]={self.probeSW_res[edgeId]:.5f}")
                self.log(f"probeNW_res[{edgeId}]={self.probeNW_res[edgeId]:.5f}")
                self.log(f"probeNE_res[{edgeId}]={self.probeNE_res[edgeId]:.5f}")
                self.log(f"probeSE_res[{edgeId}]={self.probeSE_res[edgeId]:.5f}")

                for dirId in range(4):
                    self.log(f"phi_plate_xy[{edgeId}][{dirId}]={self.phi_plate_xy[edgeId][dirId]:.5f}")
                    self.log(f"phi_xy[{edgeId}][{dirId}]={self.phi_xy[edgeId][dirId]:.5f}")

        meanphi_plate_all_xy = 0.0
        m_phi_xy = 0.0
        cnt = 0
        m_phi_plate_xy = [0.0] * 4

        for edgeId in range(4):
            cntdir = 0
            for dirId in range(4):
                # just for some statistics (overall meanvalue should be near to 90deg)
                meanphi_plate_all_xy   += self.phi_plate_xy[edgeId][dirId]
                m_phi_plate_xy[edgeId] += self.phi_plate_xy[edgeId][dirId]
                cntdir = cntdir + 1

                m_phi_xy += self.phi_xy[edgeId][dirId]
                cnt += 1
                # store true geometries of the plate here:
            m_phi_plate_xy[edgeId] /= cntdir
        meanphi_plate_all_xy /= cnt
        m_phi_xy       /= cnt

        self.log(f"meanphi_plate_all_xy={meanphi_plate_all_xy}, m_phi_xy={m_phi_xy}")

        for edgeId in range(4):
            self.log(f"m_phi_plate_xy[{edgeId}]=, {m_phi_plate_xy[edgeId]:.5f}")
        self.log(f"{m_phi_plate_xy[0]:.5f},{m_phi_plate_xy[1]:.5f},{m_phi_plate_xy[2]:.5f},{m_phi_plate_xy[3]:.5f}")
        self.log(f"m_phi_xy=, {m_phi_xy:.5f}, meanphi_plate_all_xy={meanphi_plate_all_xy:.5f}")

        self.print_skew(m_phi_xy)


    def get_angle(self,points):
        """
        returns angle_deg, std_angle_deg
            angle between x-axis and linear-fit
            positiv angle <-> CCW rotation
        Calculate line angle and its standard deviation from x,y points.

        Parameters
        ----------
        points : list of dicts
            [{'scanpos': ..., 'edge_pos': ...}, ...]

        """

        x = np.array([p['scanpos'] for p in points], dtype=float)
        y = np.array([p['edge_pos'] for p in points], dtype=float)

        # The fitted line is nearly horizontal by construction.
        # Therefore the angle calculation should remain numerically stable.
        result = linregress(x, y)
        m = result.slope
        m_std = result.stderr  # standard error of slope


        # angle
        angle_rad = np.arctan(m)
        angle_deg = np.degrees(angle_rad)

        # error propagation:
        # alpha = atan(m)
        # d(alpha)/dm = 1/(1+m²)
        angle_std_rad = m_std / (1 + m**2)
        angle_std_deg = np.degrees(angle_std_rad)

        return angle_deg, angle_std_deg


    def extract_inflection_points(self,scan_data):
        """
        Extracts the pos-coordinates of the left (rising) and right (falling)
        inflection points from an s-curve up/down dataset.

        Parameters:
        scan_data (list of dict): List containing {'pos': xval, 'freq': yval}

        Returns:
        tuple: (x1, x2) where x1 is the 1st inflection point (raising edge) and x2 is the 2nd (falling edge).
        """
        # 1. Convert input to sorted numpy arrays
        # Sorting by x ensures chronological ordering along the curve
        # TODO check, if this is needed, was given by gemini in early beginning of development ...
        sorted_data = sorted(scan_data, key=lambda k: k['pos'])
        x = np.array([pt['pos'] for pt in sorted_data])
        y = np.array([pt['freq'] for pt in sorted_data])

        if len(y) < 5:
            raise ValueError(f"too few samples: {len(y)}")

        # 2. Smooth the data to reliably find the peak and global min/max
        # Savitzky-Golay filter preserves the structure while removing noise
        window_length = max(5, len(y) // 10)
        if window_length % 2 == 0:  # Window length must be odd
            window_length += 1

        y_smooth = savgol_filter(y, window_length=window_length, polyorder=2)

        # Find the peak (split point between rising and falling edges)
        peak_idx = np.argmax(y_smooth)

        # Determine the global min and max from the smoothed data
        y_min, y_max = np.min(y_smooth), np.max(y_smooth)
        y_range = y_max - y_min

        # Define thresholds
        y_20 = y_min + 0.20 * y_range
        y_80 = y_min + 0.80 * y_range

        # 3. Split into rising and falling segments
        x_rise, y_rise = x[:peak_idx], y[:peak_idx]
        x_fall, y_fall = x[peak_idx:], y[peak_idx:]

        # 4. Filter the 20% to 80% range on the noisy data
        rise_mask = (y_rise >= y_20) & (y_rise <= y_80)
        fall_mask = (y_fall >= y_20) & (y_fall <= y_80)

        # Fallback in case noise leaves masks empty
        if not np.any(rise_mask) or not np.any(fall_mask):
            self.log(f"edge does not show 20->80% for raising or falling")
            raise ValueError("unexpected error")

        x_rise_ext, y_rise_ext = x_rise[rise_mask], y_rise[rise_mask]
        x_fall_ext, y_fall_ext = x_fall[fall_mask], y_fall[fall_mask]

        if len(x_rise_ext) < self.MIN_INFLECTION_POINTS or len(x_fall_ext) < self.MIN_INFLECTION_POINTS:
            self.log(f"Unexpected failure len(x_rise_ext)={len(x_rise_ext)}<4 OR , len(x_fall_ext)={len(x_fall_ext)} < 4")
            raise ValueError("unexpected error")


        # 5. Fit 3rd-degree polynomials (y = a*x^3 + b*x^2 + c*x + d)
        # np.polyfit returns coefficients in the order [a, b, c, d]
        poly_rise = np.polyfit(x_rise_ext, y_rise_ext, 3)
        poly_fall = np.polyfit(x_fall_ext, y_fall_ext, 3)

        # 6. Calculate inflection points: x = -b / (3*a)

        if (abs(poly_rise[0]) < self.POLYFIT_EPSILON) or (abs(poly_fall[0]) < self.POLYFIT_EPSILON):
            self.log(f"Unexpected failure poly3 fit failed to detect inflection point of scan")
            raise ValueError("unexpected error")

        x1 = -poly_rise[1] / (3 * poly_rise[0])
        x2 = -poly_fall[1] / (3 * poly_fall[0])

        return x1, x2


    def _fly_path(self, moves):
        # moves: list of ([x, y, z], speed). Klipper's lookahead slows down
        # inside the faster move, so each move runs at its own speed
        # from its start to its end.
        for target, speed in moves:
            self.toolhead.manual_move(target, speed)
        self.currx, self.curry, self.currz = moves[-1][0]
        # TODO This is coming from the code in cartographer.py .... I should try to understand or remove...
        self.toolhead.dwell(self.TOOLHEAD_DWELL_AFTER_SCAN_S)
        self.toolhead.wait_moves()


    def _xyz(self, diraxis, a, s):
        # position with 'a' on the scan axis and 's' on the sweep axis
        if diraxis == self.DIRX:
            return [a, s, self.z_height]
        return [s, a, self.z_height]


    @staticmethod
    def _sorted_arrays(scan):
        scan = sorted(scan, key=lambda k: k['pos'])
        return (np.array([p['pos'] for p in scan], dtype=float),
                np.array([p['freq'] for p in scan], dtype=float))


    @staticmethod
    def _smooth(y):
        # same smoothing as extract_inflection_points
        window_length = max(5, len(y) // 10)
        if window_length % 2 == 0:
            window_length += 1
        return savgol_filter(y, window_length=window_length, polyorder=2)


    def _scan_line(self, diraxis, s, start, segments):
        # Move to 'start' on the scan axis (sweep axis at s), then stream while
        # running through segments [(a_target, speed), ...].
        # Returns sorted (pos, freq) arrays.
        self.moveabs_to(*self._xyz(diraxis, start, s))
        moves = [(self._xyz(diraxis, a, s), v) for a, v in segments]
        scan = self._scan_path(diraxis, moves, min(v for _, v in segments))
        return self._sorted_arrays(scan)


    def _size_windows(self, x, y, e1, e2):
        # Measure the transition on a slow full-width scan and size the windows.
        # Returns (off, on, d_off, d_on, signal_range).
        ys = self._smooth(y)
        ymin, ymax = np.min(ys), np.max(ys)
        rng = ymax - ymin
        below = ys < ymin + 0.05 * rng
        above = ys > ymin + 0.95 * rng
        left = x[(x < e1) & below]
        right = x[(x > e2) & below]
        inner = x[(x > e1) & (x < e2) & above]
        if len(left) == 0 or len(right) == 0 or len(inner) == 0:
            raise ValueError("calibration line does not reach the 5% and 95% signal levels")
        d_off = max(e1 - np.max(left), np.min(right) - e2)
        d_on = max(np.min(inner) - e1, e2 - np.max(inner))

        def clamp(v):
            return min(max(v, self.EDGE_WIN_MIN), self.EDGE_WIN_MAX)

        off = self.edgeoff if self.edgeoff > 0 else clamp(self.EDGE_OFF_FACTOR * d_off)
        on = self.edgeon if self.edgeon > 0 else clamp(self.EDGE_ON_FACTOR * d_on)
        if 2 * on > e2 - e1:
            raise ValueError(f"plate ({e2 - e1:.1f}mm) too narrow for plate-side windows of {on:.1f}mm")
        return off, on, d_off, d_on, rng


    def _edge_in_window(self, x, y, center, off, on, rising, slow, cal_range):
        # Find one edge using only the samples in a window around it:
        #   rising edge  (W/S): [center-off, center+on]
        #   falling edge (E/N): [center-on,  center+off]
        # The window's own min/max define the 20-80% band, which is fitted
        # with a cubic like extract_inflection_points. The window is
        # re-centred on the found edge until it stops moving, so every line
        # uses the same window relative to its edge.
        # slow: (lo, hi) range of this line that was scanned at SCANSPEED.
        for _ in range(self.EDGE_CENTER_ITER):
            lo, hi = (center - off, center + on) if rising else (center - on, center + off)
            if lo < slow[0] - 1e-6 or hi > slow[1] + 1e-6:
                raise ValueError(f"window {lo:.1f}..{hi:.1f} outside slow section {slow[0]:.1f}..{slow[1]:.1f}")
            m = (x >= lo) & (x <= hi)
            xw, yw = x[m], y[m]
            if len(yw) < 5:
                raise ValueError(f"too few samples in window: {len(yw)}")
            ys = self._smooth(yw)
            ymin, ymax = np.min(ys), np.max(ys)
            rng = ymax - ymin
            if rng < self.EDGE_MIN_CONTRAST * cal_range:
                raise ValueError(f"low signal range in window ({rng:.0f} < {self.EDGE_MIN_CONTRAST:.0%} of {cal_range:.0f})")
            if (ys[-1] > ys[0]) != rising:
                raise ValueError("window signal runs the wrong way")
            band = (yw >= ymin + 0.2 * rng) & (yw <= ymin + 0.8 * rng)
            if np.count_nonzero(band) < self.MIN_INFLECTION_POINTS:
                raise ValueError(f"too few samples in 20-80% band: {np.count_nonzero(band)}")
            poly = np.polyfit(xw[band], yw[band], 3)
            if abs(poly[0]) < self.POLYFIT_EPSILON:
                raise ValueError("cubic fit failed")
            edge = -poly[1] / (3 * poly[0])
            if not (np.min(xw[band]) <= edge <= np.max(xw[band])):
                raise ValueError(f"inflection point {edge:.2f} outside the 20-80% band")
            if abs(edge - center) < self.EDGE_CENTER_TOL:
                return edge
            step = abs(edge - center)
            center = edge
        # Noise can make the re-centring flip between two nearly equal results.
        if step < self.EDGE_CENTER_ACCEPT:
            return center
        raise ValueError(f"window did not settle on the edge (last step {step:.3f}mm)")


    @staticmethod
    def _predict_edge(koo, s):
        # edge position at sweep position s, from the lines measured so far
        sp = [p['scanpos'] for p in koo]
        ep = [p['edge_pos'] for p in koo]
        if len(koo) >= 2:
            m, b = np.polyfit(sp, ep, 1)
            return m * s + b
        return ep[-1]


    def edge_sweep(self, diraxis, e1c, e2c, s1c, s2c):
        # EDGESCAN sweep along diraxis.
        #   e1c, e2c: coarse edges on the scan axis (W/E or S/N)
        #   s1c, s2c: coarse edges on the sweep axis
        # Returns the two edge lists, like the EDGESCAN=0 loops.
        name = "WE" if diraxis == self.DIRX else "SN"
        full_start = 1.5 * e1c - 0.5 * e2c   # same full-width range as EDGESCAN=0
        full_end = 1.5 * e2c - 0.5 * e1c
        rng = s2c - s1c
        sweep = [s1c + rng * 0.25 + (1.0 - 2.0 * 0.25) * i * rng / (self.nmeas - 1.0)
                 for i in range(self.nmeas)]

        def full_line(s):
            x, y = self._scan_line(diraxis, s, full_start, [(full_end, self.scanspeed)])
            e1f, e2f = self.extract_inflection_points([{'pos': p, 'freq': f} for p, f in zip(x, y)])
            return x, y, e1f, e2f

        koo1, koo2 = [], []
        # Calibration line: slow over the full width, sizes the windows.
        s = sweep[0]
        x, y, e1f, e2f = full_line(s)
        off, on, d_off, d_on, cal_range = self._size_windows(x, y, e1f, e2f)
        self.log(f"{name} transition: 5%..edge={d_off:.1f}mm edge..95%={d_on:.1f}mm"
                 f" -> window off-plate={off:.1f}mm plate-side={on:.1f}mm")
        full = (full_start, full_end)
        e1 = self._edge_in_window(x, y, e1f, off, on, True, full, cal_range)
        e2 = self._edge_in_window(x, y, e2f, off, on, False, full, cal_range)
        koo1.append({'scanpos': s, 'edge_pos': e1})
        koo2.append({'scanpos': s, 'edge_pos': e2})

        rescans = 0
        for i, s in enumerate(sweep[1:], start=2):
            p1 = self._predict_edge(koo1, s)
            p2 = self._predict_edge(koo2, s)
            a0 = p1 - off - self.EDGE_PAD
            a1 = p1 + on + self.EDGE_PAD
            a2 = p2 - on - self.EDGE_PAD
            a3 = p2 + off + self.EDGE_PAD
            if a2 - a1 >= self.EDGE_MIN_FAST:
                segments = [(a1, self.scanspeed), (a2, self.movespeed), (a3, self.scanspeed)]
                slow1, slow2 = (a0, a1), (a2, a3)
            else:
                segments = [(a3, self.scanspeed)]
                slow1 = slow2 = (a0, a3)
            x, y = self._scan_line(diraxis, s, a0, segments)
            try:
                e1 = self._edge_in_window(x, y, p1, off, on, True, slow1, cal_range)
                e2 = self._edge_in_window(x, y, p2, off, on, False, slow2, cal_range)
            except ValueError as err:
                # Rescan slowly over the full width. Same window detection,
                # so all lines stay comparable. If it fails again, abort.
                self.log(f"{name} line {i}/{self.nmeas}: {err}; rescanning slowly over full width")
                rescans += 1
                x, y, e1f, e2f = full_line(s)
                e1 = self._edge_in_window(x, y, e1f, off, on, True, full, cal_range)
                e2 = self._edge_in_window(x, y, e2f, off, on, False, full, cal_range)
            koo1.append({'scanpos': s, 'edge_pos': e1})
            koo2.append({'scanpos': s, 'edge_pos': e2})

        if rescans:
            self.log(f"{name}: {rescans} of {self.nmeas - 1} lines were rescanned slowly")
        return koo1, koo2


    def carto_scan(self, diraxis, dist, speed=None):
        # scan from currx,y,z to target position
        #  store sensor values and position
        #  extract the two inflection points (raising and falling s-curved edge) as position of the edges of the plate
        if diraxis == self.DIRX:
            targetx = self.currx+dist
            targety = self.curry
            targetz = self.currz
        elif diraxis == self.DIRY:
            targetx = self.currx
            targety = self.curry+dist
            targetz = self.currz
        elif diraxis == self.DIRZ:
            targetx = self.currx
            targety = self.curry
            targetz = self.currz+dist
        else:
            raise ValueError(f"carto_scan(): diraxis parameter out of range")

        if speed is None:
            speed = self.scanspeed
        curr_scan = self._scan_path(diraxis, [([targetx, targety, targetz], speed)], speed)
        pos1, pos2 = self.extract_inflection_points(curr_scan)

        # give klipper air to breath ... fix whenever I do understand, which would work :-(
#        self.printer.get_reactor().pause(self.printer.get_reactor().now() + 0.02)
#        self.printer.get_reactor().pause(time.time() + 0.02)
        return pos1, pos2


    def _scan_path(self, diraxis, moves, sample_speed):
        # Stream sensor data while running through moves [([x, y, z], speed), ...]
        # starting at currx/y/z. sample_speed (the slowest speed) sets the downsampling.
        # Returns [{'pos': position on diraxis, 'freq': ...}, ...]
        start = [self.currx, self.curry, self.currz][diraxis]
        dist = moves[-1][0][diraxis] - start

        # used for the "old" position calculation algorithm (debug output only)
        move_duration = abs(dist) / moves[0][1]

        total_samples = [0]
        # local list for the sensor data, which is collected in the callback
        raw_stream_data = []

# counter for downsampling -> avoid timeout in klipper
        sample_counter = [0]

        def cb(sample):
            total_samples[0] += 1
            sample_counter[0] += 1
            
            # test: 200 -> 1
            # 10 -> 10
            # 5 -> 20
            # Downsampling to avoid timeout error in klipper (if to many samples get read with slow scanspeeds
            modval=int(math.ceil(100/sample_speed))   # use each sample for speed>=100 (-> modval==1)
            if sample_counter[0] % modval != 0:
                return

            sample_time = sample.get('mcu_time', sample.get('time', sample.get('timestamp', 0.0)))

            raw_stream_data.append({
                'mcu_time': sample_time,
                'freq': sample['freq'],
                'pos': sample.get('pos', None)
            })

        # this seems to be private cartographer API, TODO copy/paste/modify content here
        self.scanner._start_streaming()

        # Required by the current Cartographer implementation.
        # The exact purpose is still unclear.
        self.scanner._sample_printtime_sync(5)

        with self.scanner.streaming_session(cb) as ss:
            self._fly_path(moves)

# give some cpu-power back to klipper:
        self.toolhead.wait_moves()

        if self.DEBUGPRINT:
            self.gcmd.respond_info(f"cartographer scan: Sampled {total_samples[0]} total points over")

        if not raw_stream_data:
            self.log(f"No data received: diraxis={diraxis}!")
            raise ValueError(f"carto_scan(): Scan failed to report sensor values")


        # --- Calculate the position from time ---
        first_mcu_time = raw_stream_data[0]['mcu_time']
        # sensor data for one single linear movement of the toolhead above the plate
        curr_scan = []

        for pt in raw_stream_data:
            if len(moves) == 1:
                mcu_elapsed = pt['mcu_time'] - first_mcu_time
                progress = mcu_elapsed / move_duration
                calc_pos = (dist) * progress
            else:
                # time is not proportional to distance with several speeds
                calc_pos = float('nan') if pt['pos'] is None else pt['pos'][diraxis] - start


            # Position reported together with the frequency measurement.
            # Used to determine the plate edge location.
            if pt['pos'] is None:
                self.log("pos missing in scanner data")
                raise ValueError("Scanner did not report position")
            meas_pos = pt['pos'] [diraxis]
            curr_scan.append({
                'pos': meas_pos,
                'freq': pt['freq']
            })

            # At the end, this list will be completely written to a .txt file to use inside octave
            #   scans can be differentiated by self.n
            self.allscans.append({
                'n': self.n,
                'mcu_time': pt['mcu_time'],
                'dist2start': calc_pos,
                'pos': pt['pos'],
                'freq': pt['freq']
            })
        # next scan (scans get differentiated by self.n)
        self.n += 1
        return curr_scan

    def moveabs_to(self,x,y,z):
        self.toolhead.manual_move([x, y, z], self.movespeed)
        self.currx = x
        self.curry = y
        self.currz = z
        self.toolhead.wait_moves()


    def writeEdge2file(self,fname,lin_data):
        file_path = os.path.expanduser(f"~/printer_data/gcodes/{fname}")
        try:
            with open(file_path, "w") as f:
                # no Headline (use load in octave)
                for n, pt in enumerate(lin_data):
                    f.write(f"{pt['scanpos']:.2f}, {pt['edge_pos']:.2f}\n")
            self.log(f"write line to file finished")
        except Exception as e:
            self.log(f"Could not write to file: {str(e)}")
    


    def write_scans_to_file(self):
        file_path = os.path.expanduser("~/printer_data/gcodes/measure_skew_scan_results.txt")
        try:
            with open(file_path, "w") as f:
                # no Headline (use load in octave)
                for n, pt in enumerate(self.allscans):
                    pos=pt['pos']
                    if pos is None:
                        posx = posy = posz = float('nan')
                    else:
                        posx, posy, posz = pos
                    f.write(f"{n}, {pt['n']:.5f}, {pt['mcu_time']:.5f}, {pt['dist2start']:.5f}, {posx:.5f}, {posy:.5f}, {posz:.5f}, {pt['freq']}\n")
            self.log(f"write to file finished")
        except Exception as e:
            self.log(f"Could not write to file: {str(e)}")


    def create_MEASURE_SKEW(self, gcmd):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.scanner = self.printer.lookup_object('scanner')
        self.gcmd = gcmd

        # Read command parameters.
        self.xcenter = gcmd.get_float('XCENTER', 175.0)
        self.ycenter = gcmd.get_float('YCENTER', 175.0)
        self.z_height = gcmd.get_float('ZHEIGHT', 2.0)

        self.scanspeed = gcmd.get_float('SCANSPEED', 20.0)
        self.movespeed = gcmd.get_float('MOVESPEED', 200.0)
        self.savedist = gcmd.get_float('SAVEDIST', 25.0)
        self.nmeas = gcmd.get_int('NMEAS', 9)

        # EDGESCAN=1: slow only around the edges, see EDGE_* constants
        self.edgescan = gcmd.get_int('EDGESCAN', 0, minval=0, maxval=1)
        self.edgeoff = gcmd.get_float('EDGEOFF', 0.0, minval=0.0)   # 0 = auto
        self.edgeon = gcmd.get_float('EDGEON', 0.0, minval=0.0)     # 0 = auto
        self.coarsespeed = gcmd.get_float(
            'COARSESPEED', self.COARSESPEED_EDGESCAN if self.edgescan else self.scanspeed, above=0.0)

        # get min/max coordinates of the printer.
        # reserve savedist(=20mm) buffer

        kin = self.toolhead.get_kinematics()
        self.xmin = kin.axes_min.x
        self.ymin = kin.axes_min.y
        self.xmax = kin.axes_max.x
        self.ymax = kin.axes_max.y

        if self.DEBUGPRINT:
            self.log(
                f"Detected printer range:"
                f" X[{self.xmin}, {self.xmax}]"
                f" Y[{self.ymin}, {self.ymax}]")

        self.xmin += self.savedist
        self.ymin += self.savedist
        self.xmax -= self.savedist
        self.ymax -= self.savedist

# TODO readout XCENTER and YCENTER from klipper
        self.log(f"MEASURE_SKEW XCENTER={self.xcenter} YCENTER={self.ycenter} ZHEIGHT={self.z_height} SCANSPEED={self.scanspeed} MOVESPEED={self.movespeed} SAVEDIST={self.savedist} NMEAS={self.nmeas}")
        if self.edgescan:
            self.log(f"EDGESCAN=1 COARSESPEED={self.coarsespeed}"
                     f" EDGEOFF={self.edgeoff or 'auto'} EDGEON={self.edgeon or 'auto'}")
        self.log(f"Starting. Corner (1) must point to {self.CORNER_LOCATIONS[self.meascnt - 1]}"
)

        '''
        # concept for xyskey measurement:
        # y=ycenter
        # scan x=xmin->xmax
        #  get xW/xE edge
        # x=(xW+xE)/2   move approx to the middle of the plate
        # scan y=ymin->ymax
        #  get yS/yN edge

        # for measind in range(nmeas):
        #   startx=3*xW/2-xE/2     # half width to the left from left turnpoint
        #   targetx=3*xE/2-xW/2    # half width to the right from right turnpoint
        #   starty=yS+measind*(yN-yS)/(nmeas-1.0))
        #   targety=starty
        #   startz=z_height
        #   targetz=z_height
        #   moveto start
        #   scanto target
        #   store
        '''


        # Find approximate west/east plate edges.
        self.moveabs_to(self.xmin,self.ycenter,self.z_height)
        try:
            xW, xE = self.carto_scan(self.DIRX,self.xmax-self.xmin, self.coarsespeed)
        except ValueError as e:
            self.restart_all()
            return

        # Find approximate south/north plate edges.
        self.moveabs_to((xW+xE)/2,self.ymin,self.z_height)
        try:
            yS, yN = self.carto_scan(self.DIRY,self.ymax-self.ymin, self.coarsespeed)
        except ValueError as e:
            self.restart_all()
            return

# show the range, which is used for doing the scans ... more of academic interest...
        self.log(f"result of WE and SN scans: xW={xW:.1f} xE={xE:.1f} yS={yS:.1f} yN={yN:.1f}")


        if self.edgescan:
            try:
                edg_W_koo, edg_E_koo = self.edge_sweep(self.DIRX, xW, xE, yS, yN)
                edg_S_koo, edg_N_koo = self.edge_sweep(self.DIRY, yS, yN, xW, xE)
            except ValueError as e:
                self.log(f"EDGESCAN failed: {e}. If this repeats, use EDGESCAN=0")
                self.restart_all()
                return
        else:
            # Storage for west and east edge positions.
            edg_W_koo = []
            edg_E_koo = []

            # These are the settings, which are valid for all scans in WE direction:
            startx  = 3 * xW/2 - xE/2       # half width to the left from left turnpoint
            targetx = 3 * xE/2 - xW/2       # half width to the right from right turnpoint
            rng = yN-yS
            startz  = self.z_height
            targetz = self.z_height
            stepy=rng/(self.nmeas-1.0)
            miny=yS+rng*0.25
            maxy=yS+rng*0.75
            self.log(f"Do WE scan: startx={startx:.1f} targetx={targetx:.1f} yscan={miny:.1f}:{stepy:.1f}:{maxy:.1f}")
            for measind in range(self.nmeas):
                # starty  = yS + measind * rng/(self.nmeas-1.0)
                starty  = yS+rng*0.25 + (1.0-2.0*0.25)*measind * rng/(self.nmeas-1.0)
                targety = starty
                self.moveabs_to(startx, starty, startz)
                try:
                    edg_W, edg_E = self.carto_scan(self.DIRX, targetx - startx)
                except ValueError as e:
                    self.restart_all()
                    return

                # These "coordinates" will be used for linear-regression,
                # to determine the angle of the edges (measured angle!)
                edg_W_koo.append({
                        'scanpos': starty,
                        'edge_pos': edg_W
                    })
                edg_E_koo.append({
                        'scanpos': starty,
                        'edge_pos': edg_E
                    })


            edg_S_koo = []
            edg_N_koo = []

            # These are the settings, which are valid for all scans in SN direction:
            starty  = 3 * yS/2 - yN/2       # half width to the left from left turnpoint
            targety = 3 * yN/2 - yS/2       # half width to the right from right turnpoint
            rng = xE-xW
            startz  = self.z_height
            targetz = self.z_height
            stepx=rng/(self.nmeas-1.0)
            minx=xW+rng*0.25
            maxx=xW+rng*0.75
            self.log(f"Do SN scan: startx={startx:.1f} targetx={targetx:.1f} yscan={minx:.1f}:{stepx:.1f}:{maxx:.1f}")

            for measind in range(self.nmeas):
                startx  = xW+rng*0.25 + (1.0-2.0*0.25)*measind * rng/(self.nmeas-1.0)

                targetx = startx
                self.moveabs_to(startx, starty, startz)
                try:
                    edg_S, edg_N = self.carto_scan(self.DIRY, targety - starty)
                except ValueError as e:
                    self.restart_all()
                    return

                edg_S_koo.append({
                        'scanpos': startx,
                        'edge_pos': edg_S
                    })
                edg_N_koo.append({
                        'scanpos': startx,
                        'edge_pos': edg_N
                    })

        # Get angle of W and E edges
        [ang_W, dev_ang_W] = self.get_angle(edg_W_koo)
        [ang_E, dev_ang_E] = self.get_angle(edg_E_koo)

        # Get angle of S and N edges
        [ang_S, dev_ang_S] = self.get_angle(edg_S_koo)
        [ang_N, dev_ang_N] = self.get_angle(edg_N_koo)

        if self.DEBUGSCANS2TXT:
# store edg_W_koo, etc for the documentation:
            self.writeEdge2file("edg_W_koo.txt", edg_W_koo);
            self.writeEdge2file("edg_E_koo.txt", edg_E_koo);
            self.writeEdge2file("edg_S_koo.txt", edg_S_koo);
            self.writeEdge2file("edg_N_koo.txt", edg_N_koo);



#        if self.DEBUGPRINT:
# should be printed always, I would like to have an idea about the accuracy
        self.log(f"phi_W={ang_W:.5f} +/- {dev_ang_W:.5f}")
        self.log(f"phi_E={ang_E:.5f} +/- {dev_ang_E:.5f}")
        self.log(f"phi_S={ang_S:.5f} +/- {dev_ang_S:.5f}")
        self.log(f"phi_N={ang_N:.5f} +/- {dev_ang_N:.5f}")
        self.log(f"phi_N={ang_N:.5f},phi_S={ang_S:.5f},phi_E={ang_E:.5f},phi_W={ang_W:.5f}")

        # These are the - measured! - angles of the block in the different corners.
        #  when these values are known for each rotation of the block,
        # the xyskew and real block corner angles can be calculated
        # out of them
        self.probeSW_res[(self.meascnt-1)] = 90.0-ang_S-ang_W
        self.probeNW_res[(self.meascnt-1)] = 90.0+ang_N+ang_W
        self.probeNE_res[(self.meascnt-1)] = 90.0-ang_N-ang_E
        self.probeSE_res[(self.meascnt-1)] = 90.0+ang_S+ang_E

        if (self.meascnt<4):
            self.log(f"Place (1) to {self.CORNER_LOCATIONS[self.meascnt]}")
            self.meascnt += 1
# used for documentation purpose -> check scans of a single MEASURE_SKEW instance
#            if self.DEBUGSCANS2TXT:
#                self.write_scans_to_file()
        else:
            self.calc_skew()
            if self.DEBUGSCANS2TXT:
                self.log(f"all scans finished for all 4 rotations, now write data")
                self.write_scans_to_file()
            # restart skew measurement from beginning
            self.restart_all()
        valtoprint = self.meascnt - 1
        if valtoprint==0:
            valtoprint = 4
        self.log(f"{valtoprint} measurement finished.")


# install the new command
def load_config(config):
    return SkewScanner(config)
