import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import {
  listMetadataColumns,
  listMetadataSchemas,
  listMetadataTables,
  type MetadataColumnItem,
  type MetadataSchemaItem,
  type MetadataTableItem,
} from '../api/metadata'

export interface SqlIntelligenceContext {
  datasourceId: () => string
  dbType: () => string | undefined
  defaultSchema: () => string | undefined
}

const METADATA_DB_TYPES = new Set(['mysql', 'dm', 'postgresql'])
const SIMPLE_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_$#]*$/
const IDENTIFIER_PATTERN = '(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$#]*)'
const QUALIFIED_COLUMN_PREFIX = new RegExp(
  `(${IDENTIFIER_PATTERN})\\.(${IDENTIFIER_PATTERN})\\.([A-Za-z_][A-Za-z0-9_$#]*)?$`,
)
const MEMBER_PREFIX = new RegExp(
  `(${IDENTIFIER_PATTERN})\\.([A-Za-z_][A-Za-z0-9_$#]*)?$`,
)
const TABLE_REFERENCE = new RegExp(
  `\\b(?:from|join)\\s+(${IDENTIFIER_PATTERN})(?:\\s*\\.\\s*(${IDENTIFIER_PATTERN}))?` +
    `(?:\\s+(?:as\\s+)?([A-Za-z_][A-Za-z0-9_$#]*))?`,
  'gi',
)
const SQL_KEYWORDS = [
  'SELECT',
  'FROM',
  'WHERE',
  'JOIN',
  'LEFT JOIN',
  'RIGHT JOIN',
  'INNER JOIN',
  'ON',
  'AS',
  'AND',
  'OR',
  'NOT',
  'NULL',
  'IS NULL',
  'IN',
  'EXISTS',
  'CASE',
  'WHEN',
  'THEN',
  'ELSE',
  'END',
  'GROUP BY',
  'HAVING',
  'ORDER BY',
  'LIMIT',
  'WITH',
  'UNION ALL',
] as const
const RESERVED_ALIAS_WORDS = new Set([
  'where',
  'join',
  'left',
  'right',
  'inner',
  'outer',
  'full',
  'cross',
  'on',
  'group',
  'order',
  'having',
  'limit',
  'union',
  'except',
  'intersect',
])

interface TableReference {
  schema: string
  table: string
  alias: string
}

const modelContexts = new WeakMap<monaco.editor.ITextModel, SqlIntelligenceContext>()
const schemaRequests = new Map<string, Promise<MetadataSchemaItem[]>>()
const tableRequests = new Map<string, Promise<MetadataTableItem[]>>()
const columnRequests = new Map<string, Promise<MetadataColumnItem[]>>()
let providers: monaco.IDisposable[] = []
let attachmentCount = 0

export function attachSqlIntelligence(
  editor: monaco.editor.IStandaloneCodeEditor,
  context: SqlIntelligenceContext,
): monaco.IDisposable {
  attachmentCount += 1
  ensureProviders()

  let activeModel = editor.getModel()
  if (activeModel) modelContexts.set(activeModel, context)
  const modelListener = editor.onDidChangeModel(() => {
    if (activeModel) modelContexts.delete(activeModel)
    activeModel = editor.getModel()
    if (activeModel) modelContexts.set(activeModel, context)
  })

  return {
    dispose() {
      modelListener.dispose()
      if (activeModel) modelContexts.delete(activeModel)
      attachmentCount = Math.max(0, attachmentCount - 1)
      if (attachmentCount === 0) {
        for (const provider of providers) provider.dispose()
        providers = []
      }
    },
  }
}

export function clearSqlMetadataCache(datasourceId: string): void {
  const prefix = `${datasourceId}\u0000`
  for (const cache of [schemaRequests, tableRequests, columnRequests]) {
    for (const key of cache.keys()) {
      if (key === datasourceId || key.startsWith(prefix)) cache.delete(key)
    }
  }
}

function ensureProviders(): void {
  if (providers.length > 0) return
  providers = [
    monaco.languages.registerCompletionItemProvider('sql', {
      triggerCharacters: ['.'],
      provideCompletionItems: provideCompletionItems,
    }),
    monaco.languages.registerHoverProvider('sql', {
      provideHover,
    }),
  ]
}

async function provideCompletionItems(
  model: monaco.editor.ITextModel,
  position: monaco.Position,
): Promise<monaco.languages.CompletionList> {
  const context = modelContexts.get(model)
  const word = model.getWordUntilPosition(position)
  const range = new monaco.Range(
    position.lineNumber,
    word.startColumn,
    position.lineNumber,
    position.column,
  )
  const keywordSuggestions: monaco.languages.CompletionItem[] = SQL_KEYWORDS.map((keyword) => ({
    label: keyword,
    kind: monaco.languages.CompletionItemKind.Keyword,
    insertText: keyword,
    range,
  }))
  if (!context || !metadataSupported(context)) return { suggestions: keywordSuggestions }

  const datasourceId = context.datasourceId()
  const linePrefix = model.getLineContent(position.lineNumber).slice(0, position.column - 1)
  const qualifiedColumn = QUALIFIED_COLUMN_PREFIX.exec(linePrefix)
  if (qualifiedColumn) {
    const schemaName = unquoteIdentifier(qualifiedColumn[1])
    const tableName = unquoteIdentifier(qualifiedColumn[2])
    const fragment = qualifiedColumn[3] ?? ''
    return {
      suggestions: columnSuggestions(
        await metadataColumns(datasourceId, schemaName, tableName),
        position,
        fragment,
      ),
    }
  }

  const member = MEMBER_PREFIX.exec(linePrefix)
  if (member) {
    const owner = unquoteIdentifier(member[1])
    const fragment = member[2] ?? ''
    const tableRef = findTableReferences(model.getValue()).find(
      (ref) =>
        sameIdentifier(ref.alias, owner) ||
        (Boolean(ref.schema) && sameIdentifier(ref.table, owner)),
    )
    if (tableRef) {
      const schemaName = tableRef.schema || context.defaultSchema() || ''
      if (schemaName) {
        return {
          suggestions: columnSuggestions(
            await metadataColumns(datasourceId, schemaName, tableRef.table),
            position,
            fragment,
          ),
        }
      }
    }
    return {
      suggestions: tableSuggestions(
        await metadataTables(datasourceId, owner),
        position,
        fragment,
      ),
    }
  }

  const suggestions = [...keywordSuggestions]
  const tableRefs = findTableReferences(model.getValue())
  for (const ref of tableRefs) {
    if (!ref.alias) continue
    suggestions.push({
      label: ref.alias,
      kind: monaco.languages.CompletionItemKind.Variable,
      insertText: ref.alias,
      detail: qualifiedTableName(ref.schema, ref.table),
      range,
    })
  }
  if (/\b(?:from|join)\s+[A-Za-z0-9_$#"]*$/i.test(linePrefix)) {
    for (const schema of await metadataSchemas(datasourceId)) {
      suggestions.push({
        label: schema.name,
        kind: monaco.languages.CompletionItemKind.Module,
        insertText: completionInsertText(schema.name),
        detail: 'schema',
        range,
      })
    }
  }
  return { suggestions }
}

async function provideHover(
  model: monaco.editor.ITextModel,
  position: monaco.Position,
): Promise<monaco.languages.Hover | null> {
  const context = modelContexts.get(model)
  if (!context || !metadataSupported(context)) return null
  const word = model.getWordAtPosition(position)
  if (!word) return null

  const line = model.getLineContent(position.lineNumber)
  const beforeWord = line.slice(0, word.startColumn - 1)
  const ownerMatch = new RegExp(`(${IDENTIFIER_PATTERN})\\.\\s*$`).exec(beforeWord)
  const tableRefs = findTableReferences(model.getValue())
  let schemaName = ''
  let tableName = ''

  if (ownerMatch) {
    const owner = unquoteIdentifier(ownerMatch[1])
    const ref = tableRefs.find(
      (item) => sameIdentifier(item.alias, owner) && sameIdentifier(item.table, word.word),
    )
    schemaName = ref?.schema || owner
    tableName = ref?.table || word.word
  } else {
    const ref = tableRefs.find((item) => sameIdentifier(item.table, word.word))
    if (!ref) return null
    schemaName = ref.schema || context.defaultSchema() || ''
    tableName = ref.table
  }
  if (!schemaName || !tableName) return null

  const datasourceId = context.datasourceId()
  const columns = await metadataColumns(datasourceId, schemaName, tableName)
  if (context.datasourceId() !== datasourceId || columns.length === 0) return null
  const visibleColumns = columns.slice(0, 80)
  const rows = visibleColumns.map((column) => {
    const type = column.driver_type || column.type
    return `| ${markdownCell(column.name)} | ${markdownCell(type)} | ${column.nullable ? 'YES' : 'NO'} | ${
      column.primary_key ? 'PK' : ''
    } | ${markdownCell(column.comment ?? '')} |`
  })
  if (columns.length > visibleColumns.length) {
    rows.push(`| … | ${columns.length - visibleColumns.length} more columns |  |  |  |`)
  }
  return {
    range: new monaco.Range(
      position.lineNumber,
      word.startColumn,
      position.lineNumber,
      word.endColumn,
    ),
    contents: [
      { value: `**${markdownCell(qualifiedTableName(schemaName, tableName))}**` },
      { value: ['| Column | Type | Nullable | Key | Comment |', '| --- | --- | --- | --- | --- |', ...rows].join('\n') },
    ],
  }
}

function tableSuggestions(
  tables: MetadataTableItem[],
  position: monaco.Position,
  fragment: string,
): monaco.languages.CompletionItem[] {
  const range = replacementRange(position, fragment)
  return tables.map((table) => ({
    label: table.name,
    kind: monaco.languages.CompletionItemKind.Class,
    insertText: completionInsertText(table.name),
    filterText: table.name,
    sortText: table.name.toLocaleLowerCase(),
    detail: table.table_type ? `${table.schema_name} · ${table.table_type}` : table.schema_name,
    range,
  }))
}

function columnSuggestions(
  columns: MetadataColumnItem[],
  position: monaco.Position,
  fragment: string,
): monaco.languages.CompletionItem[] {
  const range = replacementRange(position, fragment)
  return columns.map((column) => ({
    label: column.name,
    kind: monaco.languages.CompletionItemKind.Field,
    insertText: completionInsertText(column.name),
    filterText: column.name,
    sortText: `${column.primary_key ? '0' : '1'}${column.name.toLocaleLowerCase()}`,
    detail: `${column.driver_type || column.type}${column.nullable ? '' : ' · NOT NULL'}${
      column.primary_key ? ' · PK' : ''
    }`,
    documentation: column.comment || undefined,
    range,
  }))
}

function replacementRange(position: monaco.Position, fragment: string): monaco.Range {
  return new monaco.Range(
    position.lineNumber,
    position.column - fragment.length,
    position.lineNumber,
    position.column,
  )
}

function findTableReferences(sql: string): TableReference[] {
  const refs: TableReference[] = []
  TABLE_REFERENCE.lastIndex = 0
  for (const match of sql.matchAll(TABLE_REFERENCE)) {
    const first = unquoteIdentifier(match[1])
    const second = match[2] ? unquoteIdentifier(match[2]) : ''
    const candidateAlias = match[3] ?? ''
    const alias = RESERVED_ALIAS_WORDS.has(candidateAlias.toLocaleLowerCase()) ? '' : candidateAlias
    refs.push({
      schema: second ? first : '',
      table: second || first,
      alias,
    })
  }
  return refs
}

function metadataSupported(context: SqlIntelligenceContext): boolean {
  return Boolean(context.datasourceId() && METADATA_DB_TYPES.has(context.dbType() ?? ''))
}

function metadataSchemas(datasourceId: string): Promise<MetadataSchemaItem[]> {
  return cachedRequest(schemaRequests, datasourceId, () => listMetadataSchemas(datasourceId, false))
}

function metadataTables(datasourceId: string, schemaName: string): Promise<MetadataTableItem[]> {
  const key = `${datasourceId}\u0000${schemaName}`
  return cachedRequest(tableRequests, key, () => listMetadataTables(datasourceId, schemaName, false))
}

function metadataColumns(
  datasourceId: string,
  schemaName: string,
  tableName: string,
): Promise<MetadataColumnItem[]> {
  const key = `${datasourceId}\u0000${schemaName}\u0000${tableName}`
  return cachedRequest(columnRequests, key, () =>
    listMetadataColumns(datasourceId, schemaName, tableName, false),
  )
}

function cachedRequest<T>(
  cache: Map<string, Promise<T[]>>,
  key: string,
  request: () => Promise<T[]>,
): Promise<T[]> {
  const cached = cache.get(key)
  if (cached) return cached
  const pending = request().catch(() => {
    cache.delete(key)
    return []
  })
  cache.set(key, pending)
  return pending
}

function unquoteIdentifier(identifier: string): string {
  if (identifier.startsWith('"') && identifier.endsWith('"')) {
    return identifier.slice(1, -1).replaceAll('""', '"')
  }
  return identifier
}

function completionInsertText(identifier: string): string {
  return SIMPLE_IDENTIFIER.test(identifier) ? identifier : `"${identifier.replaceAll('"', '""')}"`
}

function sameIdentifier(left: string, right: string): boolean {
  return left.toLocaleLowerCase() === right.toLocaleLowerCase()
}

function qualifiedTableName(schemaName: string, tableName: string): string {
  return schemaName ? `${schemaName}.${tableName}` : tableName
}

function markdownCell(value: string): string {
  return value.replaceAll('|', '\\|').replaceAll('\n', ' ')
}
