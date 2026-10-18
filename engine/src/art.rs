//! Album covers for the terminal, made here so the UI never touches an image.
//!
//! The UI asks for Spotify's 300x300 cover (about 25 KB) at a size in terminal cells, up
//! to 256x256 pixels. It is decoded, area averaged down to the pixel grid and sent as:
//!
//! * colour blocks: each cell is an upper half block, its top pixel the foreground and its
//!   bottom pixel the background, so a cell holds two square pixels. Colours are reduced
//!   to a small palette (median cut, then a few k-means passes) and the top/bottom pairs
//!   to as many as the terminal has colour pairs free, rarer pairs taking the nearest
//!   kept one. The UI just defines those pairs and draws runs of one pair;
//! * or, for terminals without colour, ASCII text on a density ramp.
//!
//! The last cover is kept as it came, compressed (not the 270 KB of decoded pixels), so a
//! resize re-renders it without a download.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use zune_core::bytestream::ZCursor;
use zune_core::colorspace::ColorSpace;
use zune_core::options::DecoderOptions;
use zune_jpeg::JpegDecoder;

/// Dark to bright, for light text on a dark terminal (jp2a's ramp: smooth, not noisy).
const RAMP: &[u8] = b"   ...',;:clodxkO0KXNWM";
const MAX_SIDE: usize = 2048;
const PALETTE: usize = 64;
const KMEANS_PASSES: usize = 4;
/// Colours are chosen from at most this many pixels, so a big cover costs no more to plan.
const QUANT_SAMPLE: usize = 16 * 1024;
/// 256 x 256 pixels: 256 cells wide, 128 rows of two.
pub const MAX_COLS: usize = 256;
pub const MAX_ROWS: usize = 128;
/// Pair numbers go out as two hex digits per cell.
pub const MAX_PAIRS: usize = 256;

type Rgb = [u8; 3];

pub struct Image {
    w: usize,
    h: usize,
    px: Vec<u8>,
}

/// The last cover fetched, still compressed: (url, JPEG).
#[derive(Default)]
pub struct Covers(Mutex<Option<(String, Arc<Vec<u8>>)>>);

impl Covers {
    pub fn get(&self, url: &str) -> Option<Arc<Vec<u8>>> {
        self.0.lock().ok()?.as_ref().filter(|c| c.0 == url).map(|c| c.1.clone())
    }

    pub fn put(&self, url: &str, jpeg: Arc<Vec<u8>>) {
        if let Ok(mut slot) = self.0.lock() {
            *slot = Some((url.to_string(), jpeg));
        }
    }
}

pub fn decode(jpeg: &[u8]) -> Result<Image, String> {
    let options = DecoderOptions::default()
        .jpeg_set_out_colorspace(ColorSpace::RGB)
        .set_max_width(MAX_SIDE)
        .set_max_height(MAX_SIDE);
    let mut dec = JpegDecoder::new_with_options(ZCursor::new(jpeg), options);
    let px = dec.decode().map_err(|e| format!("cover image: {e:?}"))?;
    let info = dec.info().ok_or("cover image has no size")?;
    let (w, h) = (usize::from(info.width), usize::from(info.height));
    if w == 0 || h == 0 || px.len() < w * h * 3 {
        return Err("cover image is empty".into());
    }
    Ok(Image { w, h, px })
}

/// The cover area averaged down to `cols` x `rows` pixels.
fn sample(g: &Image, cols: usize, rows: usize) -> Vec<Rgb> {
    let mut out = Vec::with_capacity(cols * rows);
    for cy in 0..rows {
        let (y0, y1) = span(cy, rows, g.h);
        for cx in 0..cols {
            let (x0, x1) = span(cx, cols, g.w);
            let mut sum = [0u32; 3];
            for y in y0..y1 {
                for p in g.px[(y * g.w + x0) * 3..(y * g.w + x1) * 3].chunks_exact(3) {
                    for c in 0..3 {
                        sum[c] += u32::from(p[c]);
                    }
                }
            }
            let n = ((y1 - y0) * (x1 - x0)) as u32;
            out.push(sum.map(|s| (s / n) as u8));
        }
    }
    out
}

/// The pixels cell `i` of `n` covers along a side of `len` pixels: at least one.
fn span(i: usize, n: usize, len: usize) -> (usize, usize) {
    let a = i * len / n;
    (a.min(len - 1), ((i + 1) * len / n).max(a + 1).min(len))
}

/// `rows` lines of `cols` ASCII characters.
pub fn ascii(g: &Image, cols: usize, rows: usize) -> Value {
    let (cols, rows) = (cols.clamp(1, MAX_COLS), rows.clamp(1, MAX_ROWS));
    let cells: Vec<u8> = sample(g, cols, rows)
        .iter()
        .map(|p| ((u32::from(p[0]) * 299 + u32::from(p[1]) * 587 + u32::from(p[2]) * 114) / 1000) as u8)
        .collect();
    // Stretch the cover's own range over the whole ramp, ignoring a few outliers, so dark
    // and washed-out covers still show their shapes.
    let mut sorted = cells.clone();
    sorted.sort_unstable();
    let lo = i32::from(sorted[sorted.len() / 50]);
    let hi = i32::from(sorted[sorted.len() - 1 - sorted.len() / 50]).max(lo + 1);
    let top = (RAMP.len() - 1) as i32;
    let lines: Vec<String> = cells
        .chunks(cols)
        .map(|row| {
            row.iter()
                .map(|&v| RAMP[((i32::from(v) - lo) * top / (hi - lo)).clamp(0, top) as usize] as char)
                .collect()
        })
        .collect();
    json!({"lines": lines})
}

/// `rows` rows of `cols` half-block cells using at most `pairs` colour pairs:
/// `{"palette": ["rrggbb", ...], "pairs": [[top, bottom], ...], "rows": ["<2 hex digits a cell>", ...]}`.
pub fn blocks(g: &Image, cols: usize, rows: usize, pairs: usize) -> Value {
    let (cols, rows, pairs) = (cols.clamp(1, MAX_COLS), rows.clamp(1, MAX_ROWS), pairs.clamp(1, MAX_PAIRS));
    let px = sample(g, cols, rows * 2);
    let step = px.len().div_ceil(QUANT_SAMPLE);
    let sample: Vec<Rgb> = px.iter().step_by(step).copied().collect();
    let palette = quantize(&sample, PALETTE);
    let idx: Vec<u8> = px.iter().map(|p| nearest(&palette, p) as u8).collect();

    // Top and bottom pixel of every cell, the commonest pairs first.
    let cell = |x: usize, y: usize| (idx[2 * y * cols + x], idx[(2 * y + 1) * cols + x]);
    let mut count: HashMap<(u8, u8), u32> = HashMap::new();
    for y in 0..rows {
        for x in 0..cols {
            *count.entry(cell(x, y)).or_default() += 1;
        }
    }
    let mut kept: Vec<((u8, u8), u32)> = count.into_iter().collect();
    kept.sort_unstable_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let rare: Vec<(u8, u8)> = kept.split_off(kept.len().min(pairs)).into_iter().map(|(k, _)| k).collect();
    let mut slot: HashMap<(u8, u8), usize> = kept.iter().enumerate().map(|(i, (k, _))| (*k, i)).collect();
    for r in rare {
        let d = |k: &(u8, u8)| dist(&palette[k.0 as usize], &palette[r.0 as usize]) + dist(&palette[k.1 as usize], &palette[r.1 as usize]);
        let best = (0..kept.len()).min_by_key(|&i| d(&kept[i].0)).unwrap_or(0);
        slot.insert(r, best);
    }

    let lines: Vec<String> = (0..rows)
        .map(|y| (0..cols).map(|x| format!("{:02x}", slot[&cell(x, y)])).collect())
        .collect();
    json!({
        "palette": palette.iter().map(|c| format!("{:02x}{:02x}{:02x}", c[0], c[1], c[2])).collect::<Vec<_>>(),
        "pairs": kept.iter().map(|(k, _)| [k.0, k.1]).collect::<Vec<_>>(),
        "rows": lines,
    })
}

/// Squared distance, weighted the way the eye weighs the channels.
fn dist(a: &Rgb, b: &Rgb) -> u32 {
    let d = |i: usize| (i32::from(a[i]) - i32::from(b[i])).unsigned_abs();
    2 * d(0) * d(0) + 4 * d(1) * d(1) + 3 * d(2) * d(2)
}

fn nearest(palette: &[Rgb], p: &Rgb) -> usize {
    (0..palette.len()).min_by_key(|&i| dist(&palette[i], p)).unwrap_or(0)
}

/// At most `k` colours that represent `px` well: median cut, refined by k-means.
fn quantize(px: &[Rgb], k: usize) -> Vec<Rgb> {
    let mut boxes: Vec<Vec<Rgb>> = vec![px.to_vec()];
    while boxes.len() < k {
        // Split the box with the widest channel range, weighted by how many pixels it holds.
        let widest = |b: &Vec<Rgb>| {
            (0..3)
                .map(|c| {
                    let (lo, hi) = b.iter().fold((255u8, 0u8), |(lo, hi), p| (lo.min(p[c]), hi.max(p[c])));
                    (c, u32::from(hi - lo))
                })
                .max_by_key(|&(_, r)| r)
                .unwrap_or((0, 0))
        };
        let Some((i, c)) = boxes
            .iter()
            .enumerate()
            .filter(|(_, b)| b.len() > 1)
            .map(|(i, b)| {
                let (c, r) = widest(b);
                (i, c, u64::from(r) * b.len() as u64)
            })
            .filter(|&(_, _, score)| score > 0)
            .max_by_key(|&(_, _, score)| score)
            .map(|(i, c, _)| (i, c))
        else {
            break;
        };
        let mut b = boxes.swap_remove(i);
        b.sort_unstable_by_key(|p| p[c]);
        let upper = b.split_off(b.len() / 2);
        boxes.push(b);
        boxes.push(upper);
    }
    let mut palette: Vec<Rgb> = boxes.iter().map(|b| mean(b.iter())).collect();
    for _ in 0..KMEANS_PASSES {
        let mut sums = vec![([0u32; 3], 0u32); palette.len()];
        for p in px {
            let s = &mut sums[nearest(&palette, p)];
            for c in 0..3 {
                s.0[c] += u32::from(p[c]);
            }
            s.1 += 1;
        }
        for (dst, (sum, n)) in palette.iter_mut().zip(sums) {
            if n > 0 {
                *dst = sum.map(|v| (v / n) as u8);
            }
        }
    }
    palette
}

fn mean<'a>(px: impl Iterator<Item = &'a Rgb>) -> Rgb {
    let (mut sum, mut n) = ([0u32; 3], 0u32);
    for p in px {
        for c in 0..3 {
            sum[c] += u32::from(p[c]);
        }
        n += 1;
    }
    sum.map(|v| (v / n.max(1)) as u8)
}
