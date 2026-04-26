// Python-msgpack-compatible encoder for serde_json::Value.
// Dependencies: serde_json with `preserve_order` feature so Object iterates in insertion order.

use serde_json::Value;

pub fn encode_value(v: &Value, out: &mut Vec<u8>) {
    match v {
        Value::Null => out.push(0xc0),
        Value::Bool(b) => out.push(if *b { 0xc3 } else { 0xc2 }),
        Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                encode_uint(u, out);
            } else if let Some(i) = n.as_i64() {
                encode_int(i, out);
            } else if let Some(f) = n.as_f64() {
                encode_f64(f, out);
            } else {
                encode_f64(0.0, out);
            }
        }
        Value::String(s) => encode_str(s.as_bytes(), out),
        Value::Array(arr) => {
            encode_array_header(arr.len(), out);
            for item in arr {
                encode_value(item, out);
            }
        }
        Value::Object(map) => {
            encode_map_header(map.len(), out);
            for (k, val) in map {
                encode_str(k.as_bytes(), out);
                encode_value(val, out);
            }
        }
    }
}

fn encode_str(b: &[u8], out: &mut Vec<u8>) {
    let n = b.len();
    if n <= 31 {
        out.push(0xa0 | (n as u8));
    } else if n <= 0xff {
        out.push(0xd9);
        out.push(n as u8);
    } else if n <= 0xffff {
        out.push(0xda);
        out.extend_from_slice(&(n as u16).to_be_bytes());
    } else {
        out.push(0xdb);
        out.extend_from_slice(&(n as u32).to_be_bytes());
    }
    out.extend_from_slice(b);
}

fn encode_array_header(n: usize, out: &mut Vec<u8>) {
    if n <= 15 {
        out.push(0x90 | (n as u8));
    } else if n <= 0xffff {
        out.push(0xdc);
        out.extend_from_slice(&(n as u16).to_be_bytes());
    } else {
        out.push(0xdd);
        out.extend_from_slice(&(n as u32).to_be_bytes());
    }
}

fn encode_map_header(n: usize, out: &mut Vec<u8>) {
    if n <= 15 {
        out.push(0x80 | (n as u8));
    } else if n <= 0xffff {
        out.push(0xde);
        out.extend_from_slice(&(n as u16).to_be_bytes());
    } else {
        out.push(0xdf);
        out.extend_from_slice(&(n as u32).to_be_bytes());
    }
}

fn encode_uint(u: u64, out: &mut Vec<u8>) {
    if u <= 0x7f {
        out.push(u as u8);
    } else if u <= 0xff {
        out.push(0xcc);
        out.push(u as u8);
    } else if u <= 0xffff {
        out.push(0xcd);
        out.extend_from_slice(&(u as u16).to_be_bytes());
    } else if u <= 0xffff_ffff {
        out.push(0xce);
        out.extend_from_slice(&(u as u32).to_be_bytes());
    } else {
        out.push(0xcf);
        out.extend_from_slice(&u.to_be_bytes());
    }
}

fn encode_int(i: i64, out: &mut Vec<u8>) {
    if i >= 0 {
        encode_uint(i as u64, out);
        return;
    }
    if i >= -32 {
        out.push(i as i8 as u8);
    } else if i >= i8::MIN as i64 {
        out.push(0xd0);
        out.push(i as i8 as u8);
    } else if i >= i16::MIN as i64 {
        out.push(0xd1);
        out.extend_from_slice(&(i as i16).to_be_bytes());
    } else if i >= i32::MIN as i64 {
        out.push(0xd2);
        out.extend_from_slice(&(i as i32).to_be_bytes());
    } else {
        out.push(0xd3);
        out.extend_from_slice(&i.to_be_bytes());
    }
}

fn encode_f64(f: f64, out: &mut Vec<u8>) {
    out.push(0xcb);
    out.extend_from_slice(&f.to_be_bytes());
}
