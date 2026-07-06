// 最小 STORED(不压缩)ZIP 打包器 —— 把用户直接选的多个 .sql/.txt 文件
// 在浏览器端打包成一个 ZIP Blob,复用后端批量血缘的 ZIP 解析路径(worker
// 用 zipfile.ZipFile 读,STORED/DEFLATE 都支持),无需新增后端能力。
//
// STORED(不压缩)足够:血缘脚本都是小文本,压缩收益低,且省一个依赖/避免
// 引入压缩实现。接口见 createStoredZip。

export interface ZipEntry {
  /** ZIP 内条目名(通常是原文件名) */
  name: string
  /** 文件原始字节 */
  data: Uint8Array
}

// CRC32 查表(标准 IEEE 多项式 0xEDB88320,反射式)。惰性构建一次后复用。
let crcTable: Uint32Array | null = null
function getCrcTable(): Uint32Array {
  if (crcTable) return crcTable
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    table[n] = c >>> 0
  }
  crcTable = table
  return table
}

/** 计算一段字节的 CRC32(无符号 32 位)。 */
function crc32(data: Uint8Array): number {
  const table = getCrcTable()
  let crc = 0xffffffff
  for (let i = 0; i < data.length; i++) {
    crc = table[(crc ^ data[i]) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

/**
 * 把若干文件打包成一个未压缩(STORED)的 ZIP Blob。
 *
 * 手写 ZIP 结构(小端):每条目 local file header + 原始数据 + central
 * directory header,收尾 end-of-central-directory。compression=STORED(0),
 * flag bit 11(0x0800)声明文件名 UTF-8。空列表产出仅含 EOCD 的合法空 ZIP。
 */
export function createStoredZip(entries: ZipEntry[]): Blob {
  const encoder = new TextEncoder()

  interface Prepared {
    nameBytes: Uint8Array
    data: Uint8Array
    crc: number
    offset: number
  }

  const localParts: Uint8Array[] = []
  const prepared: Prepared[] = []
  let offset = 0

  for (const entry of entries) {
    const nameBytes = encoder.encode(entry.name)
    const data = entry.data
    const crc = crc32(data)

    // Local File Header = 30 字节固定 + 文件名 + extra(0)
    const header = new Uint8Array(30)
    const view = new DataView(header.buffer)
    view.setUint32(0, 0x04034b50, true) // 签名
    view.setUint16(4, 20, true) // version needed
    view.setUint16(6, 0x0800, true) // general purpose flag(bit 11 = UTF-8)
    view.setUint16(8, 0, true) // compression method = STORED
    view.setUint16(10, 0, true) // mod time
    view.setUint16(12, 0, true) // mod date
    view.setUint32(14, crc, true) // crc32
    view.setUint32(18, data.length, true) // compressed size
    view.setUint32(22, data.length, true) // uncompressed size
    view.setUint16(26, nameBytes.length, true) // filename length
    view.setUint16(28, 0, true) // extra length

    localParts.push(header, nameBytes, data)
    prepared.push({ nameBytes, data, crc, offset })
    offset += 30 + nameBytes.length + data.length
  }

  const centralStart = offset
  const centralParts: Uint8Array[] = []
  let centralSize = 0

  for (const p of prepared) {
    // Central Directory Header = 46 字节固定 + 文件名 + extra(0) + comment(0)
    const header = new Uint8Array(46)
    const view = new DataView(header.buffer)
    view.setUint32(0, 0x02014b50, true) // 签名
    view.setUint16(4, 20, true) // version made by
    view.setUint16(6, 20, true) // version needed
    view.setUint16(8, 0x0800, true) // general purpose flag(bit 11 = UTF-8)
    view.setUint16(10, 0, true) // compression method = STORED
    view.setUint16(12, 0, true) // mod time
    view.setUint16(14, 0, true) // mod date
    view.setUint32(16, p.crc, true) // crc32
    view.setUint32(20, p.data.length, true) // compressed size
    view.setUint32(24, p.data.length, true) // uncompressed size
    view.setUint16(28, p.nameBytes.length, true) // filename length
    view.setUint16(30, 0, true) // extra length
    view.setUint16(32, 0, true) // comment length
    view.setUint16(34, 0, true) // disk number start
    view.setUint16(36, 0, true) // internal attrs
    view.setUint32(38, 0, true) // external attrs
    view.setUint32(42, p.offset, true) // relative offset of local header

    centralParts.push(header, p.nameBytes)
    centralSize += 46 + p.nameBytes.length
  }

  // End of Central Directory Record = 22 字节固定(无 comment)
  const eocd = new Uint8Array(22)
  const eocdView = new DataView(eocd.buffer)
  eocdView.setUint32(0, 0x06054b50, true) // 签名
  eocdView.setUint16(4, 0, true) // disk number
  eocdView.setUint16(6, 0, true) // disk with central dir start
  eocdView.setUint16(8, prepared.length, true) // entries on this disk
  eocdView.setUint16(10, prepared.length, true) // total entries
  eocdView.setUint32(12, centralSize, true) // central dir size
  eocdView.setUint32(16, centralStart, true) // central dir start offset
  eocdView.setUint16(20, 0, true) // comment length

  const bytes = new Blob([...localParts, ...centralParts, eocd], {
    type: 'application/zip',
  })
  return bytes
}
