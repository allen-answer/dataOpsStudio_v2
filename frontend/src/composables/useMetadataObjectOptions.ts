import {
  computed,
  ref,
  toValue,
  watch,
  type ComputedRef,
  type MaybeRefOrGetter,
  type Ref,
} from 'vue'
import {
  listMetadataSchemas,
  listMetadataTables,
  type MetadataSchemaItem,
  type MetadataTableItem,
} from '../api/metadata'

export interface MetadataObjectOptions {
  schemas: Ref<MetadataSchemaItem[]>
  tables: Ref<MetadataTableItem[]>
  schemaOptions: ComputedRef<MetadataSchemaItem[]>
  tableOptions: ComputedRef<MetadataTableItem[]>
  schemasLoading: Ref<boolean>
  tablesLoading: Ref<boolean>
  schemasError: Ref<unknown>
  tablesError: Ref<unknown>
}

/**
 * Datasource metadata options shared by SQL and Compare pickers.
 *
 * Requests are generation-fenced: a slow response from the previous datasource/schema
 * cannot overwrite the options for the current selection. Existing persisted values are
 * kept as fallback options even when they are no longer returned by introspection.
 */
export function useMetadataObjectOptions(
  datasourceId: MaybeRefOrGetter<string>,
  schemaName: MaybeRefOrGetter<string>,
  tableName: MaybeRefOrGetter<string> = '',
  includeTables: MaybeRefOrGetter<boolean> = true,
): MetadataObjectOptions {
  const schemas = ref<MetadataSchemaItem[]>([])
  const tables = ref<MetadataTableItem[]>([])
  const schemasLoading = ref(false)
  const tablesLoading = ref(false)
  const schemasError = ref<unknown>(null)
  const tablesError = ref<unknown>(null)
  let schemasGeneration = 0
  let tablesGeneration = 0

  watch(
    () => toValue(datasourceId),
    async (id) => {
      const generation = ++schemasGeneration
      schemas.value = []
      schemasError.value = null
      if (!id) {
        schemasLoading.value = false
        return
      }
      schemasLoading.value = true
      try {
        const items = await listMetadataSchemas(id, false)
        if (generation === schemasGeneration) schemas.value = items
      } catch (error) {
        if (generation === schemasGeneration) schemasError.value = error
      } finally {
        if (generation === schemasGeneration) schemasLoading.value = false
      }
    },
    { immediate: true },
  )

  watch(
    () => [toValue(datasourceId), toValue(schemaName), toValue(includeTables)] as const,
    async ([id, schema, enabled]) => {
      const generation = ++tablesGeneration
      tables.value = []
      tablesError.value = null
      if (!enabled || !id || !schema) {
        tablesLoading.value = false
        return
      }
      tablesLoading.value = true
      try {
        const items = await listMetadataTables(id, schema, false)
        if (generation === tablesGeneration) tables.value = items
      } catch (error) {
        if (generation === tablesGeneration) tablesError.value = error
      } finally {
        if (generation === tablesGeneration) tablesLoading.value = false
      }
    },
    { immediate: true },
  )

  const schemaOptions = computed(() => {
    const current = toValue(schemaName).trim()
    if (!current || schemas.value.some((item) => item.name === current)) return schemas.value
    return [{ name: current }, ...schemas.value]
  })
  const tableOptions = computed(() => {
    const current = toValue(tableName).trim()
    if (!current || tables.value.some((item) => item.name === current)) return tables.value
    return [
      {
        schema_name: toValue(schemaName),
        name: current,
        table_type: null,
      },
      ...tables.value,
    ]
  })

  return {
    schemas,
    tables,
    schemaOptions,
    tableOptions,
    schemasLoading,
    tablesLoading,
    schemasError,
    tablesError,
  }
}
