# 로컬에서 스크린샷 캡처하기

공개 저장소에 올릴 스크린샷은 실제 시스템명·IP·담당자가 노출되면 안 돼. 로컬에서 더미 데이터로 띄운 화면을 캡처하면 마스킹 누락 위험이 없어.

## 1. 더미 데이터 생성

```bash
python3 scripts/dummy_data.py --all
```

이 명령이 `data/` 안에 `-dummy.xlsx` 파일들을 만들어:

- `data/targets-dummy.xlsx`
- `data/capacity-dummy.xlsx`
- `data/eos-dummy.xlsx`
- `data/kernel_dev-dummy.xlsx`

파일은 `.gitignore`에 등록되어 커밋되지 않음.

## 2. 더미 파일로 띄우기

```bash
# 원본을 임시로 옮기고 더미를 대신 써
mv data/targets.xlsx data/targets.xlsx.bak
cp data/targets-dummy.xlsx data/targets.xlsx

mv data/capacity.xlsx data/capacity.xlsx.bak
cp data/capacity-dummy.xlsx data/capacity.xlsx

mv data/eos.xlsx data/eos.xlsx.bak
cp data/eos-dummy.xlsx data/eos.xlsx

mv data/kernel_dev.xlsx data/kernel_dev.xlsx.bak
cp data/kernel_dev-dummy.xlsx data/kernel_dev.xlsx

# 서버 실행
uvicorn app.main:app --reload
```

또는 한 줄로:

```bash
for f in targets capacity eos kernel_dev; do
  [ -f data/$f.xlsx ] && mv data/$f.xlsx data/$f.xlsx.bak
  cp data/$f-dummy.xlsx data/$f.xlsx
done
uvicorn app.main:app --reload
```

## 3. 캡처

`http://localhost:8000` 에서 스크린샷을 찍어.

더미 데이터는:

- **시스템명**: `[IT,인프라] 주요서비스 API`, `[금융,플랫폼] 메인 DB` 등
- **호스트명**: `host-a01`, `host-b02` 등
- **IP**: `10.11.0.1`, `10.11.0.2` 등
- **담당자**: `홍길동-운영1팀`, `김영희-운영2팀` 등

도메인마다 다르니 각 탭(DR, 용량관리, EoS, 커널패치)을 돌아다니며 캡처하면 돼.

## 4. 파일 크기와 마스킹

- **크기**: 가로 1600px 내외, 파일당 500KB 이하
- **세로로 긴 표**: 상단 20~30행만 잘라내기
- **주소창**: 캡처에서 빼거나 잘라내기 (사내 호스트명이 나옴)

## 5. 원본 복원

캡처를 끝내면 원본으로 복원:

```bash
for f in targets capacity eos kernel_dev; do
  rm data/$f.xlsx
  [ -f data/$f.xlsx.bak ] && mv data/$f.xlsx.bak data/$f.xlsx
done
```

## 주의

- 더미 데이터로 띄웠을 때 **기능이 실제와 동일하게 동작하는지 확인**할 것. 판정·캐싱·리마인드 등이 정상인지 한 번은 눈으로 봐야 해.
- 원본 파일을 `.bak`로 이름을 바꿀 때는 **git add 하지 말 것** (`.bak` 파일도 이미 `.gitignore`에 등록되어 있음).
- 스크린샷 파일들(`docs/screenshots/*.png`)도 `.gitignore`에 없으니 커밋 전에 **마스킹을 끝낸 파일만 올릴 것**.
