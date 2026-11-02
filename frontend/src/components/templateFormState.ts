type WorkflowFields = {
  engine: string
  workflow_file: string
  param_mapping: Record<string, string>
  output_config: { nodes?: string[] }
}

type EngineForm = WorkflowFields & { capability: string }

export function normalizeTemplateEngineFields<T extends WorkflowFields>(form: T): T {
  if (form.engine === 'comfyui') return form
  return {
    ...form,
    workflow_file: '',
    param_mapping: {},
    output_config: { nodes: [] },
  }
}

export function applyTemplateEngine<T extends EngineForm>(form: T, engine: string): T {
  return normalizeTemplateEngineFields({
    ...form,
    engine,
    capability: '',
  })
}
