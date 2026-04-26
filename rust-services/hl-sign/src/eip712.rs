// Minimal EIP-712 hashing for Hyperliquid Agent and user-signed actions.

use tiny_keccak::{Hasher, Keccak};

pub fn keccak256(data: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    let mut k = Keccak::v256();
    k.update(data);
    k.finalize(&mut out);
    out
}

pub fn agent_domain_separator() -> [u8; 32] {
    let type_hash = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    );
    let name_hash = keccak256(b"Exchange");
    let version_hash = keccak256(b"1");
    let mut chain_id = [0u8; 32];
    // 1337 = 0x539 -> last two bytes
    chain_id[30] = 0x05;
    chain_id[31] = 0x39;
    let verifying = [0u8; 32];

    let mut buf = Vec::with_capacity(32 * 5);
    buf.extend_from_slice(&type_hash);
    buf.extend_from_slice(&name_hash);
    buf.extend_from_slice(&version_hash);
    buf.extend_from_slice(&chain_id);
    buf.extend_from_slice(&verifying);
    keccak256(&buf)
}

pub fn agent_struct_hash(source: &str, connection_id: &[u8; 32]) -> [u8; 32] {
    let type_hash = keccak256(b"Agent(string source,bytes32 connectionId)");
    let source_hash = keccak256(source.as_bytes());
    let mut buf = Vec::with_capacity(32 * 3);
    buf.extend_from_slice(&type_hash);
    buf.extend_from_slice(&source_hash);
    buf.extend_from_slice(connection_id);
    keccak256(&buf)
}

pub fn agent_digest(source: &str, connection_id: &[u8; 32]) -> [u8; 32] {
    let ds = agent_domain_separator();
    let sh = agent_struct_hash(source, connection_id);
    let mut buf = Vec::with_capacity(2 + 32 + 32);
    buf.push(0x19);
    buf.push(0x01);
    buf.extend_from_slice(&ds);
    buf.extend_from_slice(&sh);
    keccak256(&buf)
}

pub fn user_signed_domain_separator(chain_id_u64: u64) -> [u8; 32] {
    let type_hash = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    );
    let name_hash = keccak256(b"HyperliquidSignTransaction");
    let version_hash = keccak256(b"1");
    let mut chain_id = [0u8; 32];
    chain_id[24..32].copy_from_slice(&chain_id_u64.to_be_bytes());
    let verifying = [0u8; 32];
    let mut buf = Vec::with_capacity(32 * 5);
    buf.extend_from_slice(&type_hash);
    buf.extend_from_slice(&name_hash);
    buf.extend_from_slice(&version_hash);
    buf.extend_from_slice(&chain_id);
    buf.extend_from_slice(&verifying);
    keccak256(&buf)
}
