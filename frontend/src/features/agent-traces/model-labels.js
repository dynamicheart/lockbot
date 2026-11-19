// Display labels only; stored model and run identities remain unchanged.
export function displayModelName(model) {
  const aliases = {
    'training13-codex · official:gpt-6.1-sol':'GPT-6.1 Sol',
    'ducc · DeepSeek-V4.1-Flash':'DeepSeek V4.1 Flash',
    'ducc · GLM-5.3':'GLM 5.3',
    'ducc · Kimi K3':'Kimi K3',
    'ducc · MiniMax-M3':'MiniMax M3',
    'ducc-luna · gpt-6-luna':'Luna',
    'ducc-kimi · Kimi K3':'Kimi K3',
    'ducc-glm · GLM-5.3':'GLM 5.3',
    'ducc-deepseek · DeepSeek-V4.1-Flash':'DeepSeek V4.1 Flash',
  };
  for (const [raw,name] of Object.entries(aliases)) {
    if (model === raw || model?.startsWith(raw + ' · ')) return name + model.slice(raw.length);
  }
  return model;
}
