<template>
  <div class="json-mapping-editor" :style="{ '--json-editor-rows': rows }">
    <div ref="host" />
    <small :id="hintId">{{ $t('jsonEditor.tabHint') }}</small>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Compartment, EditorState, EditorSelection } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, insertNewlineAndIndent } from '@codemirror/commands'
import { indentOnInput, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language'
import { json } from '@codemirror/lang-json'

const props = defineProps({
  modelValue: { type: String, default: '' },
  example: { type: String, required: true },
  label: { type: String, required: true },
  rows: { type: Number, default: 4 },
  disabled: Boolean,
})
const emit = defineEmits(['update:modelValue'])
const host = ref(null)
const hintId = useId()
const editable = new Compartment()
let view

const editing = () => [
  EditorState.readOnly.of(props.disabled),
  EditorView.editable.of(!props.disabled),
]

function insertExample(editor) {
  const { doc, readOnly } = editor.state
  if (readOnly || doc.toString().trim()) return false
  editor.dispatch({
    changes: { from: 0, to: doc.length, insert: props.example },
    selection: EditorSelection.cursor(0),
    userEvent: 'input',
  })
  return true
}

onMounted(() => {
  view = new EditorView({
    parent: host.value,
    doc: props.modelValue,
    extensions: [
      json(),
      indentOnInput(),
      history(),
      syntaxHighlighting(defaultHighlightStyle),
      EditorView.lineWrapping,
      editable.of(editing()),
      EditorState.changeFilter.of((transaction) => transaction.newDoc.length <= 131072),
      EditorView.contentAttributes.of({
        'aria-label': props.label,
        'aria-describedby': hintId,
        spellcheck: 'false',
      }),
      keymap.of([
        { key: 'Tab', run: insertExample },
        { key: 'Enter', run: insertNewlineAndIndent },
        ...defaultKeymap,
        ...historyKeymap,
      ]),
      EditorView.updateListener.of(({ docChanged, state }) => {
        if (!docChanged) return
        const entry = state.doc.toString()
        if (entry !== props.modelValue) emit('update:modelValue', entry)
      }),
    ],
  })
})

watch(
  () => props.modelValue,
  (value) => {
    if (view && view.state.doc.toString() !== value) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: value } })
    }
  }
)
watch(
  () => props.disabled,
  () => view?.dispatch({ effects: editable.reconfigure(editing()) })
)
onBeforeUnmount(() => view?.destroy())
</script>

<style scoped>
.json-mapping-editor {
  min-width: 0;
}
.json-mapping-editor :deep(.cm-editor) {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.6;
  min-height: calc(var(--json-editor-rows) * 1.6em + 16px);
  max-height: 290px;
}
.json-mapping-editor :deep(.cm-content) {
  padding: 7px 8px;
}
.json-mapping-editor :deep(.cm-focused) {
  outline: 1px solid var(--el-color-primary);
}
.json-mapping-editor small {
  display: block;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-top: 5px;
}
</style>
