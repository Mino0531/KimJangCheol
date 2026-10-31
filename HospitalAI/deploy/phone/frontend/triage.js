/** Conservative phrase guard; this does not determine medical severity. */
const normalize = (text) => String(text || '').normalize('NFKC').toLocaleLowerCase('ko')
  .replace(/[^\p{L}\p{N}]/gu, '');

const critical = [
  '119', '숨을못쉬', '숨못쉬', '숨을쉴수없', '숨을쉴수가없', '숨이안쉬어', '숨안쉬어', '숨이막혀', '의식이없', '의식을잃', '쓰러졌', '쓰러져있', '말을못하',
  '심한가슴통증', '가슴이쥐어짜', '심한출혈', '피를많이흘', '피가많이나', '피가멈추지않', '피가안멈',
  '죽고싶', '자살', '목숨을끊', '한쪽마비', '한쪽이마비', '얼굴이마비',
  '심한흉통', '피를토',
];
const finalOnly = ['호흡곤란', '가슴통증', '가슴이아파', '가슴이아프', '가슴아파', '가슴아프', '흉통'];
const pesticide = ['농약', '살충제', '제초제'];
const dizziness = ['어지럽', '어지러', '어질', '현기증'];
const ingestion = ['마셨', '마셨어', '마시', '마신', '먹었', '먹은', '먹어', '삼켰', '삼킨', '삼켜', '복용', '입에들어갔', '입에들어왔', '입에들어가', '흡입했', '흡입하', '들이마셨', '들이마시'];
const vomiting = ['토하', '토해', '토했', '토한', '구토', '구역질', '메스껍', '몸이떨', '몸떨'];

function mentioned(text, words, { rejectUncertain = false } = {}) {
  return words.some((word) => {
    let index = text.indexOf(word);
    while (index !== -1) {
      const prefix = text.slice(Math.max(0, index - 18), index);
      const suffix = text.slice(index + word.length, index + word.length + 14);
      const prefixNegated = /(?:^|지만|는데|고|는|은|가|제가|저는|전혀|아예|요)안$/u.test(prefix);
      const doubleNegative = /^(?:은|는|이|가|도|을|를)?(?:전혀|아예)?없(?:지|지는|진)(?:않|안)/u.test(suffix)
        || /^(?:지|지는|진)(?:않|안)(?:은|는)?(?:건|게|것은)아니/u.test(suffix)
        || /^(?:운|는)(?:게|건|것은)아니/u.test(suffix)
        || (prefixNegated && /^(?:지|지는|진)(?:않|안)/u.test(suffix));
      const denied = !doubleNegative && (
        prefixNegated || /^(?:은|는|이|가|도|을|를)?(?:지금은|전혀|아예)?(?:없|적없|적이없|아니|아닌|아닙|아냐|지않|지는않|지도않|진않|지안|지는안|지도안|진안|리지않|고있지않|고있지는않|고있진않|안뿌리|안뿌렸|안썼|안써|사용하지않|뿌리지않)/u.test(suffix)
        || /^(?:는것은|는건|다는건|다는말은)(?:아니|아닙|아냐)/u.test(suffix)
      );
      const uncertain = rejectUncertain && /^(?:는지|는건지|나|는가|나요)(?:모르|몰라|몰라도|요|까)?/u.test(suffix);
      // Suppress only when the distant-time marker belongs directly to this
      // symptom (optionally separated by a person), not an earlier condition.
      const old = /(?:작년|재작년|지난해|예전|옛날|오래전|몇년전|수년전|십년전|과거)(?:에|에는|에도|쯤)?(?:(?:우리|저희)?(?:어머니|아버지|할머니|할아버지|엄마|아빠|남편|아내|가족|제가|내가|저는)(?:가|는|께서)?)?$/u.test(prefix);
      if (!denied && !old && !uncertain) return true;
      index = text.indexOf(word, index + word.length);
    }
    return false;
  });
}

export function pesticideUrgent(text, { partial = false } = {}) {
  const value = normalize(text);
  const signs = partial ? ['어지러워요', '어지러워', '어지럽습니다', '어질어질해요', '현기증이나요'] : dizziness;
  if (!mentioned(value, pesticide)) return false;
  const verbs = partial ? ['마셨어요', '마셨습니다', '먹었어요', '먹었습니다', '삼켰어요', '삼켰습니다', '입에들어갔어요', '입에들어왔어요', '흡입했어요', '흡입했습니다', '들이마셨어요', '들이마셨습니다'] : ingestion;
  const directIngestion = pesticide.flatMap((name) => ['', '을', '를', '이', '가'].flatMap((particle) =>
    ['', '실수로', '잘못', '조금', '한모금', '방금', '어제'].flatMap((modifier) =>
      verbs.map((verb) => `${name}${particle}${modifier}${verb}`))));
  const vomitingSigns = partial ? ['토하고있어요', '토해요', '구토해요', '구토합니다', '구역질이나요', '몸이떨려요'] : vomiting;
  return mentioned(value, directIngestion, { rejectUncertain: true })
    || mentioned(value, signs) || mentioned(value, vomitingSigns);
}

export function urgentText(text, { partial = false, includeNumber = true } = {}) {
  const value = normalize(text);
  const phrases = partial ? [...critical, '가슴이아파요', '가슴이아픕니다', '가슴아파요', '가슴아픕니다'] : [...critical, ...finalOnly];
  return mentioned(value, phrases.filter((word) => includeNumber || word !== '119'))
    || pesticideUrgent(value, { partial });
}
