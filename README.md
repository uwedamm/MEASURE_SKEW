

# MEASURE_SKEW
Klipper Python extension to measure xy-skew using approx squared aluminium plate.

# Preconditions (HW)
To use this script, you need a 50x50 mm to 150x150 mm aluminium plate or block.  
MEASURE_SKEW was tested on Voron 2.4 350x350, feedback for other devices is welcome!  
Cartographer - or other - inductive probe, to detect the edges of the aluminium plate  
Klipper v0.12.0-456. Probably other versions work also fine, but I did not test that

The main principle is to
a) Measure orientation of the 4 edges of a squared aluminium plate
b) do same in 4 directions (rotation of the plate by 90° for each measurement)
c) calculate the 4 corner angles of the cube (just for information, currently not needed)
d) calculate the xy-skew and printout the correction functionality for printer.cfg

## How to use (Basic workflow)
- Switch on the Printer.
- CHOME  (G28) (home all 3 axis)
- QGL (quad gantry level)
- G1 X175 Y175 Z60  (set your own z-homing position here, should be approximately in the middle of the bed)
- place the aluminium plate below cartographer (carto approx middle of the plate), nearly orthogonal with the build plate
- G28 z (home on the top of the plate), Z-homing xy coordinates MUST be approx middle of the plate)
- MEASURE_SKEW XCENTER=175.0 YCENTER=175.0 ZHEIGHT=2.0 SCANSPEED=20 MOVESPEED=200 SAVEDIST=25.0 NMEAS=9
  default values are given, corresponding parameters can be skipped
- After measurement is finished, rotate the aluminium plate by 90° around the Z-axis.
- Repeat the previous two steps four times.

## Principle of the Measurement

Scanning the aluminium plate with the Cartographer probe provides
the measured directions of the four edges of the aluminium plate.

By repeating the measurement after rotating the aluminium plate by 90°,
both the corner angles of the aluminium plate and the XY skew of the
printer can be determined.

### How does this work?

Let's perform a simple thought experiment.

In the figure below, one corner of an aluminium plate is shown with
a corner angle that is far from being perfectly orthogonal.

Assume first that the printer has **no XY skew**, meaning that the
X and Y axes are exactly perpendicular to each other.

In this case, both measurements will report the same measured angle,
which corresponds directly to the actual corner angle of the aluminium plate:

$$
\alpha_1 = \alpha_2 = \alpha
$$

![image alt](https://github.com/uwedamm/MEASURE_SKEW/blob/6370b14421278fa220de1d9e08ca6065606281be/pictures/skewNone.png)

Now consider the opposite case: the aluminium plate itself is perfectly square, but the
printer exhibits XY skew, meaning that the angle between the X and Y axes is smaller than 90°.

![image alt](https://github.com/uwedamm/MEASURE_SKEW/blob/409d795366974f0b7dcc318511d79b359f655a36/pictures/skewWith.png)

When the corner of the aluminium plate points towards the south-east (SE), the measured angle is

$$
\alpha_1 < 90^\circ
$$

After rotating the aluminium plate by 90°, the same corner produces the opposite result:

$$
\alpha_2 > 90^\circ
$$

The true corner angle of the aluminium plate can then be calculated as the average of the two measurements:

$$
\text{plate-angle} = \frac{\alpha_1 + \alpha_2}{2}
$$

The XY skew angle is obtained from half the difference between the measurements:

$$
\text{xy-skew} = \frac{\alpha_1 - \alpha_2}{2}
$$

In the actual implementation, all four corners are measured with the aluminium
plate oriented towards SW, SE, NE, and NW. The resulting calculations are
therefore slightly more involved, but the underlying principle remains exactly the same:

- Averaging the measurements isolates the geometry of the aluminium plate.
- Taking the difference between the measurements isolates the printer's XY skew.
To perform the measurement, MEASURE_SKEW need to be executed 4 times, rotating the plate CCW by 90° each.
After the 4th MEASURE_SKEW was executed, the python prints out the measurement results to the console.


## Experimental results

tbd



## Comparison to other concepts
### print calibration object and measure with calliper  
[xy-skew-calibration-test-part](https://www.printables.com/model/67374-xy-skew-calibration-test-part)  
[vernier-skew-test](https://www.printables.com/model/1143218-vernier-skew-test)  
[calibration-bro-califlower-calilantern-calibration](https://www.printables.com/model/164261-calibration-bro-califlower-calilantern-calibration/files)  
#### Pro:
- no need for aluminium plate
- no need for cartographer
- simple onetime 3D print, single measurement using calliper  
#### Con:
- printing time
- new material (filament) needed for each measurement
- Filament shrinkage, over-extrusion at the corners, and caliper inaccuracy.
   
### Optical print calibration sheet and observe using camera  
[camera-based-skew-correction-with-charuco-improves-precision-in-3d-printing-and-cnc](https://3druck.com/en/diy/camera-based-skew-correction-with-charuco-improves-precision-in-3d-printing-and-cnc-05149845/)  
#### Pro:
- no need for aluminium plate
- no need for cartographer
- one single scan
#### Con:  
- Camera in need
- Printout of calibration paper neccessary
- unknown accuracy

### MEASURE_SKEW (This project)
#### Pro
- accuracy
- inexpensive equipment (<10,- Eur per 100x100x10mm aluminiumplate
#### Con
- cartographer in need
- aluminium plate in need
- 4 measurements need attendence of the user
- abstract mathematical calculation in background, not trivial to follow


## Challenges
All measurements and evaluations assume linearity of the x- and y-axes,as well as for the edges of the aluminum plate. The xy-skew measurement likely varies depending on the position where it is measured.

## LoP:  
- double check, that the sign of xyskew is correct
- do more measurements and compare results (play with scanspeed, Z_height, NMEAS)
- try newest version of klipper
- compare my cartographer.py, etc. with github, new version exist?
- measure linearity of x/y axis
- clarify, if the private APIs I used from cartographer.py are stable for future use


## Disclaimer
All ideas, concepts and algorithms are developed by my own. If I violate any copyright, etc. please hold me informed.

Please double-check, if the code is compatible to your printer. It was only tested on mine:
- Voron 2.4 350x350
- Cartographer equipped
- z-homing position 175x175
- 10mm thick, 100x100mm aluminium plate  

APIs used from other packages:
- printer.lookup_object('scanner')
- printer.lookup_object('gcode')
- printer.lookup_object('toolhead')

- toolhead.manual_move(pos, speed)
- toolhead.dwell(<seconds>)
- toolhead.wait_moves

- scanner._start_streaming
- scanner._sample_printtime_sync(???)
- scanner.streaming_session(callback)

- gcmd.respond_info(string)

- gcode.register_command(cmdname,cmd,description)  

Currently there is no experience on any other platform


