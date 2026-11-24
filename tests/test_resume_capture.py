from cloakup.resume_capture import professional_sections


def test_sections_exclude_personal_header_and_preserve_professional_details():
 text='姓名甲\n女 30岁\n个人优势\n多年招聘交付经验\n工作经历\n2020-2025 某企业\n负责寻访、项目管理\n教育经历\n某大学 专业\n技能特长\n人才地图\n举报\n网站页脚'
 result=professional_sections(text)
 assert set(result)=={'个人优势','工作经历','教育经历','技能特长'}
 assert '负责寻访' in result['工作经历']
 assert '姓名甲' not in str(result) and '30岁' not in str(result) and '网站页脚' not in str(result)


def test_no_recognized_sections_does_not_invent_resume():
 assert professional_sections('页面没有可读在线简历，需要授权')=={}
