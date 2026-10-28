/**
 * 语句分类(与后端 app/dbclients/sql_guard.py classify_executable_sql 同口径)。
 *
 * 前端只用来决定执行路径和确认弹窗:写语句(DML / DDL)按序走 job 路径,
 * prod 数据源先二次确认。是否真能执行以后端为准(allow_dml 闸门 + 再分类)。
 */
export type SqlStatementKind = 'query' | 'dml' | 'ddl' | 'other'

export interface ClassifiedStatement {
  kind: SqlStatementKind
  /** 首关键字小写;空语句为 ''。 */
  verb: string
  /** UPDATE / DELETE 不带 WHERE(整表改 / 删)。 */
  missingWhere: boolean
}

const DML_VERBS = new Set(['insert', 'update', 'delete', 'merge'])
const DDL_VERBS = new Set(['create', 'alter', 'drop', 'truncate'])

export function classifyStatement(sql: string): ClassifiedStatement {
  const body = stripLeadingComments(sql)
  const verb = (/^[A-Za-z_][A-Za-z0-9_]*/.exec(body)?.[0] ?? '').toLowerCase()
  let kind: SqlStatementKind = 'other'
  if (verb === 'select' || verb === 'with') kind = 'query'
  else if (DML_VERBS.has(verb)) kind = 'dml'
  else if (DDL_VERBS.has(verb)) kind = 'ddl'
  const missingWhere =
    (verb === 'update' || verb === 'delete') && !/\bwhere\b/i.test(blankLiteralsAndComments(body))
  return { kind, verb, missingWhere }
}

export function isWriteStatement(statement: ClassifiedStatement): boolean {
  return statement.kind === 'dml' || statement.kind === 'ddl'
}

function stripLeadingComments(sql: string): string {
  let rest = sql.trimStart()
  for (;;) {
    if (rest.startsWith('--')) {
      const newline = rest.indexOf('\n')
      if (newline < 0) return ''
      rest = rest.slice(newline + 1).trimStart()
    } else if (rest.startsWith('/*')) {
      const end = rest.indexOf('*/')
      if (end < 0) return ''
      rest = rest.slice(end + 2).trimStart()
    } else {
      return rest
    }
  }
}

function blankLiteralsAndComments(sql: string): string {
  let out = ''
  let quote: string | null = null
  let index = 0
  while (index < sql.length) {
    const char = sql[index]
    const next = sql[index + 1] ?? ''
    if (quote) {
      if (char === quote) quote = null
      out += ' '
      index += 1
      continue
    }
    if (char === '-' && next === '-') {
      const newline = sql.indexOf('\n', index)
      const end = newline < 0 ? sql.length : newline
      out += ' '.repeat(end - index)
      index = end
      continue
    }
    if (char === '/' && next === '*') {
      const close = sql.indexOf('*/', index + 2)
      const end = close < 0 ? sql.length : close + 2
      out += ' '.repeat(end - index)
      index = end
      continue
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char
      out += ' '
      index += 1
      continue
    }
    out += char
    index += 1
  }
  return out
}
