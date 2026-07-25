// Rodent-client field enclosure — baseplate + standoffs + cable clips
// Two-board layout: Raspberry Pi 4B and a separate GSM HAT sit side by side,
// with the RGB + thermal cameras on posts at the front. Wiring stays as-is;
// the plate's job is to immobilize every board and cable so the friction-fit
// jumper/ribbon/USB connections can't work loose in the field.
//
// WORKFLOW: measure the values in the "MEASURE THESE" block, set them, render,
// export STL. Everything downstream is parametric off these.
//
// Units: millimetres. Designed for M2.5 screws into brass heat-set inserts
// (recommended) or self-tapping into the plastic standoffs.

$fn = 48;

// ─────────────────────────────────────────────────────────────────────────
// MEASURE THESE (defaults are placeholders — confirm against your hardware)
// ─────────────────────────────────────────────────────────────────────────

// Raspberry Pi 4B — these are the real published values, leave as-is.
pi_w        = 85;      // board length
pi_h        = 56;      // board width
pi_hole_dx  = 58;      // hole spacing (long axis)
pi_hole_dy  = 49;      // hole spacing (short axis)
pi_hole_in  = 3.5;     // hole inset from board edge
pi_hole_d   = 2.7;     // M2.5 clearance / self-tap pilot

// GSM HAT — MEASURE YOURS. Assumes 4 corner holes on a rectangle.
gsm_w       = 65;      // board length  (Waveshare GSM/GPRS/GNSS HAT ~ 65 x 30 mm; VERIFY)
gsm_h       = 30;      // board width
gsm_hole_dx = 58;      // hole spacing long axis (VERIFY — many HATs mirror the Pi's 58x49)
gsm_hole_dy = 23;      // hole spacing short axis (VERIFY)
gsm_hole_in = 3.5;
gsm_hole_d  = 2.7;

// Camera modules — MEASURE. Simple post-with-hole mounts; refine to your bracket.
cam_post_h  = 18;      // how high the camera sits above the plate
cam_hole_d  = 2.2;     // M2 for small camera boards

// ─────────────────────────────────────────────────────────────────────────
// Plate + layout
// ─────────────────────────────────────────────────────────────────────────
gap          = 12;                         // gap between Pi and GSM board zones
margin       = 8;                          // border around everything
standoff_h   = 6;                          // lift boards off the plate (clears solder tails)
standoff_od  = 6;
plate_t      = 3;

// ─────────────────────────────────────────────────────────────────────────
// Shell (walls + lid) + apertures + ports
// ─────────────────────────────────────────────────────────────────────────
wall_t       = 3;
wall_h       = 46;     // interior height above the plate — clear the tallest part
                       // (Pi ports ~16 mm, camera posts ~21 mm) with airflow headroom
lid_t        = 3;
lid_lip_h    = 5;      // downward lip that nests inside the walls
fit_gap      = 0.35;   // clearance so the lid actually drops in when printed

// Camera apertures — front wall (y = 0 side). Left post = RGB, right = thermal.
// These x's match the two camera_post() calls in the assembly below.
cam_lens_z   = plate_t + cam_post_h - 3;   // approx lens-centre height
rgb_ap       = 14;     // square-ish RGB window opening (put acrylic behind it)
rgb_rebate   = 2;      // ledge depth the acrylic sits on
therm_ap_d   = 11;     // thermal aperture — OPEN, no glazing (glass blinds LWIR)
hood_drop    = 9;      // how far the thermal hood overhangs downward

// Side-wall ports (x = 0 wall). Positions are approximate — slide to your board.
usbc_y       = 12;  usbc_z = plate_t + 3;   // Pi USB-C power
usbc_w       = 11;  usbc_h = 6;
sma_y        = plate_h - 18; sma_z = wall_h/2;  // GSM antenna bulkhead
sma_d        = 7;
sd_y         = 30;  sd_z = plate_t + 1;     // SD-card access slot
sd_w         = 15;  sd_h = 3.5;

// Ventilation
vent_slot_w  = 3;   vent_slot_len = 16;  vent_count = 5;

// Overall plate size derived from the two board footprints stacked in Y.
plate_w = margin*2 + max(pi_w, gsm_w);
plate_h = margin*2 + pi_h + gap + gsm_h;

// Origin helpers
pi_x0  = margin;                 pi_y0  = margin;
gsm_x0 = margin;                 gsm_y0 = margin + pi_h + gap;

module standoff(d_hole) {
    difference() {
        cylinder(h = standoff_h, d = standoff_od);
        translate([0,0,-0.1]) cylinder(h = standoff_h+0.2, d = d_hole);
    }
}

// four standoffs on a hole rectangle placed at (x0,y0) = board corner
module board_standoffs(x0, y0, board_w, board_h, hole_dx, hole_dy, hole_in, hole_d) {
    // hole rectangle is inset hole_in from the board edges, centred on the footprint
    cx = x0 + hole_in;
    cy = y0 + hole_in;
    for (px = [cx, cx + hole_dx])
        for (py = [cy, cy + hole_dy])
            translate([px, py, plate_t]) standoff(hole_d);
}

// A comb/clip that traps a cable bundle against the plate. Print, then the wires
// snap under the fingers — keeps DuPont jumpers seated without soldering.
module cable_clip(len = 20, w = 8, post_h = 9) {
    clip_t = 2.5;
    difference() {
        union() {
            // two uprights + a roof = a slot the bundle sits in
            translate([0,       0, plate_t]) cube([clip_t, w, post_h]);
            translate([len-clip_t,0, plate_t]) cube([clip_t, w, post_h]);
            translate([0, 0, plate_t+post_h-clip_t]) cube([len, w, clip_t]);
        }
    }
}

// Front camera posts — two side by side so RGB + thermal share a view.
module camera_post(x, y) {
    translate([x, y, 0]) difference() {
        cylinder(h = plate_t + cam_post_h, d = standoff_od+2);
        translate([0,0,plate_t+cam_post_h-6])
            cylinder(h = 6.1, d = cam_hole_d);
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Assembly
// ─────────────────────────────────────────────────────────────────────────
// camera x-positions (shared by the posts and the front-wall apertures)
cam_x_rgb   = plate_w/2 - 12;   // left  post → RGB
cam_x_therm = plate_w/2 + 12;   // right post → thermal

module baseplate() {
    // base plate
    cube([plate_w, plate_h, plate_t]);

    // board standoffs
    board_standoffs(pi_x0,  pi_y0,  pi_w,  pi_h,  pi_hole_dx,  pi_hole_dy,  pi_hole_in,  pi_hole_d);
    board_standoffs(gsm_x0, gsm_y0, gsm_w, gsm_h, gsm_hole_dx, gsm_hole_dy, gsm_hole_in, gsm_hole_d);

    // cable clips: one over the GPIO jumper bundle (between the two boards),
    // one near the CSI ribbon at the Pi's front edge. Positions are rough —
    // slide them to match where your bundles actually run.
    translate([pi_x0 + 20, pi_y0 + pi_h + 1, 0]) cable_clip(len = 26);   // jumper bundle
    translate([pi_x0 + 5,  pi_y0 - 0.5,      0]) cable_clip(len = 22);   // ribbon clamp

    // two camera posts at the front margin, ~20 mm apart
    camera_post(cam_x_rgb,   4);
    camera_post(cam_x_therm, 4);
}

// ─────────────────────────────────────────────────────────────────────────
// Walls + apertures + ports
// ─────────────────────────────────────────────────────────────────────────

// A row of vent slots on a wall face, laid out along X, at height z.
module vent_row(z) {
    step = (plate_w - vent_slot_len) / (vent_count - 1);
    for (i = [0 : vent_count - 1])
        translate([i * step + (vent_slot_len)/2 - vent_slot_len/2, 0, z])
            cube([vent_slot_len, wall_t + 2, vent_slot_w]);
}

module shell() {
    difference() {
        // wall ring: outer solid minus the interior footprint (leaves the plate exposed)
        union() {
            difference() {
                translate([-wall_t, -wall_t, 0])
                    cube([plate_w + 2*wall_t, plate_h + 2*wall_t, wall_h]);
                translate([0, 0, -1])
                    cube([plate_w, plate_h, wall_h + 2]);
            }
            // thermal hood: a small exterior overhang so rain/dust/stray light
            // don't drop straight into the OPEN thermal aperture.
            translate([cam_x_therm - therm_ap_d/2 - 3, -wall_t - 6, cam_lens_z])
                difference() {
                    cube([therm_ap_d + 6, 6, hood_drop]);
                    // undercut so it prints without support and sheds water
                    translate([-1, -1, -1]) rotate([-20,0,0])
                        cube([therm_ap_d + 8, 10, hood_drop]);
                }
        }

        // ── Front-wall camera apertures (y ≈ 0 wall) ──
        // RGB: square window with an interior rebate to seat an acrylic pane.
        translate([cam_x_rgb - rgb_ap/2, -wall_t - 1, cam_lens_z - rgb_ap/2])
            cube([rgb_ap, wall_t + 2, rgb_ap]);
        translate([cam_x_rgb - (rgb_ap+3)/2, rgb_rebate, cam_lens_z - (rgb_ap+3)/2])
            cube([rgb_ap + 3, wall_t, rgb_ap + 3]);   // rebate ledge (from inside)
        // Thermal: plain open round hole (NO window — LWIR won't pass glass/acrylic).
        translate([cam_x_therm, 1, cam_lens_z]) rotate([90,0,0])
            cylinder(h = wall_t + 2, d = therm_ap_d);

        // ── Left-wall ports (x ≈ 0 wall) ──
        translate([-wall_t - 1, usbc_y, usbc_z]) cube([wall_t + 2, usbc_w, usbc_h]);      // USB-C power
        translate([1, sma_y, sma_z]) rotate([0,-90,0]) cylinder(h = wall_t + 2, d = sma_d); // SMA antenna
        translate([-wall_t - 1, sd_y, sd_z]) cube([wall_t + 2, sd_w, sd_h]);              // SD access slot

        // ── LOW intake vents on both long walls (front + back) ──
        // Cool air in low; the lid vents let warm air out high → chimney flow.
        translate([0, -wall_t - 1, 5]) vent_row(0);                    // front wall, low
        translate([0, plate_h - 1, 5]) vent_row(0);                    // back wall, low
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Lid — drop-in with a nesting lip and HIGH exhaust vents
// ─────────────────────────────────────────────────────────────────────────
module lid() {
    translate([0, 0, wall_h]) {
        // top plate (covers the full outer footprint)
        difference() {
            translate([-wall_t, -wall_t, 0])
                cube([plate_w + 2*wall_t, plate_h + 2*wall_t, lid_t]);
            // exhaust vents through the lid
            for (i = [0 : vent_count - 1])
                translate([margin + i * ((plate_w - 2*margin) / (vent_count-1)) - vent_slot_w/2,
                           plate_h/2 - vent_slot_len/2, -1])
                    cube([vent_slot_w, vent_slot_len, lid_t + 2]);
        }
        // nesting lip pointing down into the box
        translate([fit_gap, fit_gap, -lid_lip_h])
            difference() {
                cube([plate_w - 2*fit_gap, plate_h - 2*fit_gap, lid_lip_h]);
                translate([wall_t, wall_t, -1])
                    cube([plate_w - 2*wall_t - 2*fit_gap, plate_h - 2*wall_t - 2*fit_gap, lid_lip_h + 2]);
            }
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Output selector — set `part` then Render (F6) and export STL.
//   "base" = plate + standoffs + clips + posts + walls  (print this the right way up)
//   "lid"  = lid only  (print flat, vents-up)
//   "all"  = everything, for a preview of the assembled box
// ─────────────────────────────────────────────────────────────────────────
part = "all";

if (part == "base" || part == "all") { baseplate(); shell(); }
if (part == "lid"  || part == "all") { lid(); }
