// Drop-in Rust replacement for mpdex.exchanges.hl_sign
// Bit-for-bit match with Python reference:
//   msgpack(action) || nonce_be8 || (00 | 01+vault) || [00 expires_be8]
//   -> keccak256 -> connectionId
//   -> EIP-712 Agent(source="a"|"b", connectionId) sign with wallet private key
//   -> return {"r": 0x..., "s": 0x..., "v": 27|28}

mod msgpack_enc;
mod eip712;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use serde_json::{Map, Value};

use k256::ecdsa::{RecoveryId, Signature as K256Sig, SigningKey};

// ---------- Python <-> serde_json::Value conversion (preserves insertion order) ----------

fn py_to_value(obj: &Bound<PyAny>) -> PyResult<Value> {
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = obj.downcast::<PyBool>() {
        return Ok(Value::Bool(b.is_true()));
    }
    if let Ok(i) = obj.downcast::<PyInt>() {
        if let Ok(v) = i.extract::<i64>() {
            return Ok(Value::from(v));
        }
        if let Ok(v) = i.extract::<u64>() {
            return Ok(Value::from(v));
        }
        return Err(PyValueError::new_err("integer out of range for msgpack (must fit i64/u64)"));
    }
    if let Ok(f) = obj.downcast::<PyFloat>() {
        let v = f.extract::<f64>()?;
        return Ok(serde_json::Number::from_f64(v)
            .map(Value::Number)
            .unwrap_or(Value::Null));
    }
    if let Ok(s) = obj.downcast::<PyString>() {
        return Ok(Value::String(s.to_str()?.to_owned()));
    }
    if let Ok(list) = obj.downcast::<PyList>() {
        let mut arr = Vec::with_capacity(list.len());
        for item in list.iter() {
            arr.push(py_to_value(&item)?);
        }
        return Ok(Value::Array(arr));
    }
    if let Ok(t) = obj.downcast::<PyTuple>() {
        let mut arr = Vec::with_capacity(t.len());
        for item in t.iter() {
            arr.push(py_to_value(&item)?);
        }
        return Ok(Value::Array(arr));
    }
    if let Ok(d) = obj.downcast::<PyDict>() {
        let mut map = Map::new();
        for (k, v) in d.iter() {
            let key = k
                .downcast::<PyString>()
                .map_err(|_| PyTypeError::new_err("msgpack dict keys must be strings"))?
                .to_str()?
                .to_owned();
            map.insert(key, py_to_value(&v)?);
        }
        return Ok(Value::Object(map));
    }
    if let Ok(_b) = obj.downcast::<PyBytes>() {
        return Err(PyTypeError::new_err("bytes inside action dict is not supported (Hyperliquid actions are JSON-safe)"));
    }
    Err(PyTypeError::new_err(format!(
        "unsupported Python type for msgpack encoding: {}",
        obj.get_type().name()?
    )))
}

// ---------- Core signing primitives ----------

fn address_to_bytes(address: &str) -> PyResult<[u8; 20]> {
    let s = if let Some(rest) = address.strip_prefix("0x").or_else(|| address.strip_prefix("0X")) {
        rest
    } else {
        address
    };
    let raw = hex::decode(s).map_err(|e| PyValueError::new_err(format!("bad address hex: {}", e)))?;
    if raw.len() != 20 {
        return Err(PyValueError::new_err(format!(
            "address must be 20 bytes, got {}",
            raw.len()
        )));
    }
    let mut out = [0u8; 20];
    out.copy_from_slice(&raw);
    Ok(out)
}

fn compute_action_hash(
    action: &Value,
    vault_address: Option<&str>,
    nonce: u64,
    expires_after: Option<u64>,
) -> PyResult<[u8; 32]> {
    let mut buf = Vec::with_capacity(256);
    msgpack_enc::encode_value(action, &mut buf);
    buf.extend_from_slice(&nonce.to_be_bytes());
    match vault_address {
        None => buf.push(0x00),
        Some(addr) => {
            buf.push(0x01);
            let a = address_to_bytes(addr)?;
            buf.extend_from_slice(&a);
        }
    }
    if let Some(e) = expires_after {
        buf.push(0x00);
        buf.extend_from_slice(&e.to_be_bytes());
    }
    Ok(eip712::keccak256(&buf))
}

fn parse_private_key(key: &str) -> PyResult<SigningKey> {
    let s = if let Some(rest) = key.strip_prefix("0x").or_else(|| key.strip_prefix("0X")) {
        rest
    } else {
        key
    };
    let raw = hex::decode(s).map_err(|e| PyValueError::new_err(format!("bad private key hex: {}", e)))?;
    if raw.len() != 32 {
        return Err(PyValueError::new_err(format!(
            "private key must be 32 bytes, got {}",
            raw.len()
        )));
    }
    SigningKey::from_slice(&raw)
        .map_err(|e| PyValueError::new_err(format!("invalid secp256k1 private key: {}", e)))
}

fn sign_digest(sk: &SigningKey, digest: &[u8; 32]) -> PyResult<(K256Sig, RecoveryId)> {
    // k256 sign_prehash_recoverable uses deterministic RFC6979 (matches eth_account)
    sk.sign_prehash_recoverable(digest)
        .map_err(|e| PyValueError::new_err(format!("sign failed: {}", e)))
}

fn sig_to_rsv(sig: K256Sig, rec: RecoveryId) -> (String, String, u8) {
    let (r, s) = sig.split_bytes();
    // eth_utils.to_hex(int) strips leading zeros. Python hl_sign passes
    // signed["r"]/signed["s"] which are ints. Match by trimming leading zero
    // nibbles from the hex string. Empty value is encoded as "0x0".
    let r_hex = format_int_hex(&r);
    let s_hex = format_int_hex(&s);
    let v = 27u8 + rec.to_byte();
    (r_hex, s_hex, v)
}

fn format_int_hex(bytes: &[u8]) -> String {
    let full = hex::encode(bytes);
    let trimmed = full.trim_start_matches('0');
    if trimmed.is_empty() {
        "0x0".to_string()
    } else {
        format!("0x{}", trimmed)
    }
}

// ---------- Public API (PyO3) ----------

#[pyfunction]
#[pyo3(signature = (action, vault_address, nonce, expires_after))]
fn action_hash<'py>(
    py: Python<'py>,
    action: &Bound<'py, PyAny>,
    vault_address: Option<String>,
    nonce: u64,
    expires_after: Option<u64>,
) -> PyResult<Bound<'py, PyBytes>> {
    let v = py_to_value(action)?;
    let h = compute_action_hash(&v, vault_address.as_deref(), nonce, expires_after)?;
    Ok(PyBytes::new_bound(py, &h))
}

#[pyfunction]
#[pyo3(signature = (wallet_private_key, action, vault_address, nonce, expires_after, is_mainnet))]
fn sign_l1_action<'py>(
    py: Python<'py>,
    wallet_private_key: &str,
    action: &Bound<'py, PyAny>,
    vault_address: Option<String>,
    nonce: u64,
    expires_after: Option<u64>,
    is_mainnet: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let v = py_to_value(action)?;
    let h = compute_action_hash(&v, vault_address.as_deref(), nonce, expires_after)?;
    let source = if is_mainnet { "a" } else { "b" };
    let digest = eip712::agent_digest(source, &h);
    let sk = parse_private_key(wallet_private_key)?;
    let (sig, rec) = sign_digest(&sk, &digest)?;
    let (r, s, vv) = sig_to_rsv(sig, rec);
    let d = PyDict::new_bound(py);
    d.set_item("r", r)?;
    d.set_item("s", s)?;
    d.set_item("v", vv)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(signature = (wallet_private_key, action, payload_types, primary_type))]
fn sign_user_typed<'py>(
    py: Python<'py>,
    wallet_private_key: &str,
    action: &Bound<'py, PyDict>,
    payload_types: Vec<(String, String)>,
    primary_type: &str,
) -> PyResult<Bound<'py, PyDict>> {
    use tiny_keccak::{Hasher, Keccak};
    let mut type_sig = String::from(primary_type);
    type_sig.push('(');
    for (i, (name, ty)) in payload_types.iter().enumerate() {
        if i > 0 {
            type_sig.push(',');
        }
        type_sig.push_str(ty);
        type_sig.push(' ');
        type_sig.push_str(name);
    }
    type_sig.push(')');
    let type_hash = eip712::keccak256(type_sig.as_bytes());

    let mut buf = Vec::with_capacity(32 * (1 + payload_types.len()));
    buf.extend_from_slice(&type_hash);
    for (name, ty) in payload_types.iter() {
        let val = action
            .get_item(name)?
            .ok_or_else(|| PyValueError::new_err(format!("action missing field {}", name)))?;
        encode_eip712_field(&val, ty, &mut buf)?;
    }
    let struct_hash = eip712::keccak256(&buf);

    let chain_hex: String = action
        .get_item("signatureChainId")?
        .ok_or_else(|| PyValueError::new_err("action.signatureChainId missing"))?
        .extract()?;
    let chain_id = u64::from_str_radix(chain_hex.trim_start_matches("0x"), 16)
        .map_err(|e| PyValueError::new_err(format!("bad signatureChainId: {}", e)))?;
    let ds = eip712::user_signed_domain_separator(chain_id);

    let mut digest_buf = Vec::with_capacity(66);
    digest_buf.push(0x19);
    digest_buf.push(0x01);
    digest_buf.extend_from_slice(&ds);
    digest_buf.extend_from_slice(&struct_hash);
    let mut digest = [0u8; 32];
    {
        let mut k = Keccak::v256();
        k.update(&digest_buf);
        k.finalize(&mut digest);
    }

    let sk = parse_private_key(wallet_private_key)?;
    let (sig, rec) = sign_digest(&sk, &digest)?;
    let (r, s, vv) = sig_to_rsv(sig, rec);
    let d = PyDict::new_bound(py);
    d.set_item("r", r)?;
    d.set_item("s", s)?;
    d.set_item("v", vv)?;
    Ok(d)
}

fn encode_eip712_field(val: &Bound<PyAny>, ty: &str, out: &mut Vec<u8>) -> PyResult<()> {
    match ty {
        "string" => {
            let s: String = val.extract()?;
            let h = eip712::keccak256(s.as_bytes());
            out.extend_from_slice(&h);
        }
        "address" => {
            let s: String = val.extract()?;
            let raw = address_to_bytes(&s)?;
            let mut padded = [0u8; 32];
            padded[12..].copy_from_slice(&raw);
            out.extend_from_slice(&padded);
        }
        "uint8" | "uint16" | "uint32" | "uint64" | "uint256" => {
            if let Ok(n) = val.extract::<u64>() {
                let mut padded = [0u8; 32];
                padded[24..].copy_from_slice(&n.to_be_bytes());
                out.extend_from_slice(&padded);
            } else if let Ok(s) = val.extract::<String>() {
                let stripped = s.trim_start_matches("0x");
                let n = u128::from_str_radix(stripped, 16)
                    .map_err(|e| PyValueError::new_err(format!("bad uint hex: {}", e)))?;
                let mut padded = [0u8; 32];
                padded[16..].copy_from_slice(&n.to_be_bytes());
                out.extend_from_slice(&padded);
            } else {
                return Err(PyValueError::new_err("unsupported uint value for field".to_string()));
            }
        }
        "bytes32" => {
            let b: Vec<u8> = val.extract()?;
            if b.len() != 32 {
                return Err(PyValueError::new_err("bytes32 must be 32 bytes"));
            }
            out.extend_from_slice(&b);
        }
        other => {
            return Err(PyValueError::new_err(format!(
                "unsupported EIP-712 field type: {}",
                other
            )));
        }
    }
    Ok(())
}

#[pymodule]
fn mpdex_hl_sign(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(action_hash, m)?)?;
    m.add_function(wrap_pyfunction!(sign_l1_action, m)?)?;
    m.add_function(wrap_pyfunction!(sign_user_typed, m)?)?;
    m.add("__version__", "0.1.0")?;
    Ok(())
}
