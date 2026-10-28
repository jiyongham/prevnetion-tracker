# tests/test_kernel_patch_level.py
"""
커널 패치 완료 판정 - Polestar OS 패치 레벨을 OS별 타겟 커널과 비교한다.

'목록에 있으면 완료'로 보던 때는 `config[OS 패치 레벨]="4.18.0-553%"` 한 패턴에
RHEL 8.10 서버가 통째로 걸려(99대 중 88대) '원래 그 버전'을 완료로 집어냈다.
타겟이 OS 계열마다 다르므로(8.x=4.18 / 9.x=5.14) 값을 비교해야 한다.
"""
from app.config import settings
from app.services.kernel import (
    judge_kernel,
    meets_target,
    normalize_kernel,
    parse_os_major,
    target_kernel_for,
)

T8 = "kernel-4.18.0-553.164.1.el8_10"
T9 = "kernel-5.14.0-687.49.1.el9_8"


class TestParseOsMajor:
    def test_엑셀_표기(self):
        assert parse_os_major("Linux_Redhat9.7") == "9"
        assert parse_os_major("Linux_Redhat8.10") == "8"

    def test_표기가_흔들려도_읽는다(self):
        assert parse_os_major("RHEL 8.10") == "8"
        assert parse_os_major("Redhat 9.8") == "9"

    def test_리눅스가_아니면_빈_값(self):
        assert parse_os_major("Windows 2019") == ""
        assert parse_os_major("") == ""


class TestNormalizeKernel:
    def test_접두어와_아키텍처_꼬리를_떼고_비교한다(self):
        # 엑셀 타겟은 'kernel-', Polestar는 '.x86_64'가 붙어서 온다
        assert normalize_kernel(T9) == normalize_kernel("5.14.0-687.49.1.el9_8.x86_64")


class TestMeetsTarget:
    def test_타겟과_같으면_충족(self):
        assert meets_target("5.14.0-687.49.1.el9_8.x86_64", T9) is True

    def test_타겟보다_높으면_충족(self):
        # 다음 차수로 더 올라간 서버를 미완료로 되돌리면 안 된다
        assert meets_target("5.14.0-687.50.2.el9_8.x86_64", T9) is True

    def test_타겟보다_낮으면_미달(self):
        assert meets_target("5.14.0-687.48.1.el9_8.x86_64", T9) is False

    def test_el_계열이_다르면_비교하지_않는다(self):
        # el9_6 -> el9_8은 마이너 올림이라 숫자만으로 판단하면 안 된다
        assert meets_target("5.14.0-570.12.1.el9_6.x86_64", T9) is False
        assert meets_target("4.18.0-553.164.1.el8_10.x86_64", T9) is False

    def test_못_읽는_표기는_미달로_둔다(self):
        # 완료는 부풀면 되돌리기 어려우니 애매하면 미완료
        assert meets_target("unknown", T9) is False
        assert meets_target("", T9) is False


class TestTargetKernelFor:
    def test_os_계열에_맞는_타겟을_고른다(self):
        assert target_kernel_for({"os": "Linux_Redhat8.10"}) == T8
        assert target_kernel_for({"os": "Linux_Redhat9.7"}) == T9

    def test_타겟이_없는_os는_빈_값(self):
        assert target_kernel_for({"os": "Windows 2019"}) == ""


class TestJudgeKernel:
    item = {"hostname": "web01", "os": "Linux_Redhat9.7", "excel_done": ""}

    def test_엑셀_완료여부가_완료면_완료(self):
        done, reason = judge_kernel({**self.item, "excel_done": "완료"}, {})
        assert done is True and reason == "완료표기"

    def test_타겟에_도달하면_완료(self):
        done, reason = judge_kernel(self.item, {"web01": "5.14.0-687.49.1.el9_8.x86_64"})
        assert done is True and "패치 확인" in reason

    def test_미달이면_현재와_타겟을_같이_알려준다(self):
        done, reason = judge_kernel(self.item, {"web01": "5.14.0-570.12.1.el9_6.x86_64"})
        assert done is False
        assert "5.14.0-570.12.1.el9_6" in reason and "5.14.0-687.49.1.el9_8" in reason

    def test_polestar에_없으면_미완료(self):
        for source in ({}, None):
            done, reason = judge_kernel(self.item, source)
            assert done is False
            assert reason.startswith("확인 불가")

    def test_값이_빈_예전_형식_파일은_목록_기준으로_완료(self):
        # 키가 없는 것(패치 안 됨)과 값이 빈 것(패치됐으나 값 모름)은 다르다
        done, reason = judge_kernel(self.item, {"web01": ""})
        assert done is True and reason == "패치 확인 (목록 기준)"

    def test_타겟이_없는_os는_값을_보여주고_미완료로_둔다(self, monkeypatch):
        monkeypatch.setattr(settings, "kernel_target_versions", "8=" + T8)
        done, reason = judge_kernel(self.item, {"web01": "5.14.0-687.49.1.el9_8.x86_64"})
        assert done is False and "타겟 미설정" in reason

    def test_os_release_is_not_kernel_version(self):
        done, reason = judge_kernel(self.item, {"web01": "Red Hat Enterprise Linux release 9.7 (Plow)"})
        assert done is False
        assert "커널 버전 형식 아님" in reason

    def test_development_rhel8_uses_810_target(self):
        item = {**self.item, "os": "Linux_Redhat8.7"}
        assert target_kernel_for(item) == T8
        assert judge_kernel(item, {"web01": "4.18.0-553.164.1.el8_10.x86_64"})[0] is True
        assert judge_kernel(item, {"web01": "4.18.0-553.163.1.el8_10.x86_64"})[0] is False

    def test_development_rhel9_uses_98_target(self):
        assert target_kernel_for(self.item) == T9
        assert judge_kernel(self.item, {"web01": "5.14.0-687.49.1.el9_8.x86_64"})[0] is True
