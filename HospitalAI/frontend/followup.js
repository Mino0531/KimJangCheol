/** Detect explicit requests for more information before recommending a hospital. */
export function needsFollowUp(text) {
  if (/[?？]/u.test(text)) return true;
  return text.split(/[.!。\n]+/u).some((sentence) => {
    const asks = /(?:알려|말씀해|말해|답해)\s*주(?:세요|시겠어요|실래요|십시오|실\s*수\s*(?:있나요|있을까요))/u.test(sentence);
    const directedToProvider = /(?:병원|의료진|의사|간호사|119|응급실)(?:에|에게|으로)?[^.!?？\n]{0,60}(?:알려|말씀해|말해)/u.test(sentence);
    return asks && !directedToProvider;
  });
}
