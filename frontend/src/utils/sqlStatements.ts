export interface SqlStatementSegment {
  sql: string
  start: number
  end: number
}

/**
 * Split a SQL editor buffer on top-level semicolons.
 *
 * The backend deliberately accepts one read-only statement per job. Keeping the
 * split in the client lets a console run several SELECTs while every statement
 * still passes through the existing server-side guard independently.
 */
export function splitSqlStatements(source: string): SqlStatementSegment[] {
  const statements: SqlStatementSegment[] = []
  let start = 0
  let quote: "'" | '"' | '`' | null = null
  let dollarQuote = ''
  let inLineComment = false
  let inBlockComment = false
  let index = 0

  const pushSegment = (end: number): void => {
    let segmentStart = start
    let segmentEnd = end
    while (segmentStart < segmentEnd && /\s/.test(source[segmentStart] ?? '')) segmentStart += 1
    while (segmentEnd > segmentStart && /\s/.test(source[segmentEnd - 1] ?? '')) segmentEnd -= 1
    if (segmentStart < segmentEnd) {
      statements.push({ sql: source.slice(segmentStart, segmentEnd), start: segmentStart, end: segmentEnd })
    }
  }

  while (index < source.length) {
    const char = source[index]
    const next = source[index + 1] ?? ''

    if (inLineComment) {
      if (char === '\n') inLineComment = false
      index += 1
      continue
    }
    if (inBlockComment) {
      if (char === '*' && next === '/') {
        inBlockComment = false
        index += 2
      } else {
        index += 1
      }
      continue
    }
    if (dollarQuote) {
      if (source.startsWith(dollarQuote, index)) {
        index += dollarQuote.length
        dollarQuote = ''
      } else {
        index += 1
      }
      continue
    }
    if (quote) {
      if (char === quote) {
        if (source[index + 1] === quote && quote !== '`') {
          index += 2
          continue
        }
        if (!isBackslashEscaped(source, index) || quote === '`') quote = null
      }
      index += 1
      continue
    }

    if (char === '-' && next === '-') {
      inLineComment = true
      index += 2
      continue
    }
    if (char === '/' && next === '*') {
      inBlockComment = true
      index += 2
      continue
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char
      index += 1
      continue
    }
    if (char === '$') {
      const match = /^\$[A-Za-z_][A-Za-z0-9_]*\$|^\$\$/.exec(source.slice(index))
      if (match) {
        dollarQuote = match[0]
        index += dollarQuote.length
        continue
      }
    }
    if (char === ';') {
      pushSegment(index)
      start = index + 1
    }
    index += 1
  }

  pushSegment(source.length)
  return statements
}

export function statementAtOffset(source: string, offset: number): SqlStatementSegment | null {
  const statements = splitSqlStatements(source)
  return (
    statements.find((statement) => offset >= statement.start && offset <= statement.end) ??
    statements.find((statement) => statement.start >= offset) ??
    statements.at(-1) ??
    null
  )
}

function isBackslashEscaped(source: string, index: number): boolean {
  let backslashes = 0
  for (let cursor = index - 1; cursor >= 0 && source[cursor] === '\\'; cursor -= 1) backslashes += 1
  return backslashes % 2 === 1
}
