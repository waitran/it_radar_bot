"""Selection regression cases with a fixed clock; no network or Telegram token."""
import copy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup

from app.config import Config, ConfigurationError
from app.filters import Filters, parse_deadline, detect_status
from app.models import Item
from app.main import Bot
from app.collectors.telegram_channel import _parse_page
from app.collectors.website_http import text

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo('Europe/Moscow'))


def item(title, body='', **kwargs):
    return Item('test', 'Test', '1', 'https://example.org/1', title, body, None,
                kwargs.pop('published_at', NOW), raw_text=body, **kwargs)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.rules = Config.load(str(ROOT/'config.yaml')).rules
        self.filters = Filters(self.rules)

    def decide(self, title, body='', **kwargs):
        it = item(title, body, **kwargs)
        accepted, reason = self.filters.check(it, now=NOW)
        return accepted, reason, it

    def test_actionable_categories(self):
        cases = [
            ('Открыт набор на стажировку Python', 'Для студентов без опыта', 'internships'),
            ('Приглашаем на митап по SQL', 'Разберём транзакции', 'events'),
            ('Регистрируйтесь на хакатон по робототехнике', 'Заявки до 15 ноября', 'contests'),
            ('Приглашаем на день карьеры IT', 'Встреча студентов с работодателями', 'career'),
        ]
        for title, body, category in cases:
            with self.subTest(category=category):
                accepted, reason, it = self.decide(title, body)
                self.assertTrue(accepted, reason)
                self.assertIn(category, it.categories)

    def test_non_it_contests_and_corporate_news(self):
        for title, body in [
            ('Конкурс фотографий котиков для студентов', 'Приглашаем участвовать'),
            ('Наши студенты в университете', 'Компания поздравляет выпускников'),
            ('Встреча любителей футбола', 'Приглашаем в IT-компанию'),
            ('Обновили позиционирование IT-компании', 'Расскажите о вашей стажировке'),
            ('Вакансии — тема конференции', 'Мы обсудили стажировки по Python'),
        ]:
            with self.subTest(title=title):
                self.assertFalse(self.decide(title, body)[0])

    def test_mentions_without_invitation_do_not_pass(self):
        self.assertFalse(self.decide('Конференция по Python', 'Наши эксперты обсуждают её программу')[0])
        self.assertFalse(self.decide('Мы посетили конференцию по Python', 'Регистрация была открыта')[0])
        self.assertFalse(self.decide('Смотрите запись вебинара по SOC', 'Присоединяйтесь к просмотру')[0])

    def test_information_posts_are_not_invitations(self):
        cases = [
            ('Навыки ИИ-агента: что это и зачем они нужны',
             'В карточках разбираем регистрацию на конференции и обучение Python.'),
            ('Новости рабочей недели',
             'В университете проходит много событий. Конференция по ИИ 30 октября.'),
            ('ИБ-пророки обсудили будущее безопасности на форуме',
             'Эксперты рассказали про SQL. Следующая конференция 30 октября.'),
            ('Курс на очный этап',
             'Завершился отборочный этап хакатона по IT. Финал 30 октября.'),
            ('Как устроена регистрация на конференцию Python',
             'Рассказываем об интерфейсе и нейросетях.'),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                self.assertFalse(self.decide(title, body)[0])

    def test_information_with_explicit_new_opportunity_is_kept(self):
        cases = [
            ('В карточках программа митапа Python', 'Приглашаем участвовать!'),
            ('В карточках программа митапа Python', 'Регистрация по ссылке в описании'),
            ('Итоги недели и новые хакатоны по ИИ', 'Регистрация открыта. Заявки до 30 октября'),
            ('Обсудили Python на конференции', 'Приходите на следующий митап 30 октября'),
            ('Завершился отборочный этап хакатона IT', 'Присоединяйтесь к открытому финалу 30 октября'),
            ('Открыт набор на стажировку Python', 'В карточках условия для начинающих'),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                accepted, reason, _ = self.decide(title, body)
                self.assertTrue(accepted, reason)

    def test_teacher_audience_is_distinct_from_teacher_speaker(self):
        self.assertFalse(self.decide('Митапы для преподавателей вузов',
                                     'Приходите обсудить обучение Python')[0])
        self.assertFalse(self.decide('Как подружить вузы и ИТ-индустрию?',
                                     'Митапы для тех, кто развивает современное высшее образование. Приходите!')[0])
        self.assertTrue(self.decide('Митап Python для преподавателей и студентов',
                                    'Приходите обсудить машинное обучение')[0])
        self.assertTrue(self.decide('Приглашаем студентов на митап Python',
                                    'Преподаватели вузов расскажут о машинном обучении')[0])

    def test_closed_registration_spellings(self):
        for phrase in ['прием заявок закрыт', 'приём заявок закрыт', 'регистрация закрыта',
                       'набор завершён', 'заявки больше не принимаются', 'все места заняты']:
            with self.subTest(phrase=phrase):
                accepted, _, it = self.decide('Стажировка Python для студентов', phrase)
                self.assertFalse(accepted)
                self.assertEqual(it.registration_status, 'closed')

    def test_not_open_is_not_reported_open(self):
        accepted, _, it = self.decide('Регистрация на митап пока не открыта, следите за дедлайном')
        self.assertFalse(accepted)
        self.assertEqual(it.registration_status, 'unknown')

    def test_open_requires_explicit_claim_and_unique_future_deadline(self):
        for body, expected in [
            ('Регистрация открыта. Заявки до 15 ноября', 'open'),
            ('Заявки до 15 ноября', 'unknown'),
            ('Регистрация открыта', 'unknown'),
            ('Дедлайн 15 ноября. Регистрация пока не открыта', 'unknown'),
            ('Регистрация открыта. Заявки до 15 ноября; заявки до 20 ноября', 'unknown'),
        ]:
            with self.subTest(body=body):
                _, _, it = self.decide('Приглашаем на митап Python', body)
                self.assertEqual(it.registration_status, expected)

    def test_transaction_and_license_are_not_ads(self):
        self.assertTrue(self.decide('Приглашаем на IT-митап', 'Разбираем транзакции и лицензирование ПО')[0])
        self.assertFalse(self.decide('Скидка 50% на продукт Python', 'Купите лицензию сейчас')[0])

    def test_giveaway_is_not_a_technical_competition(self):
        self.assertFalse(self.decide('Конкурс в честь Python-митапа', 'Разыгрываем наушники, подпишись')[0])

    def test_beginner_vacancies(self):
        for title, experience in [('Стажер-аналитик SOC', ''), ('Junior Python Developer', ''),
                                  ('Инженер поддержки Linux', 'Без опыта 0-1 год')]:
            with self.subTest(title=title):
                accepted, reason, it = self.decide(title, 'Технические обязанности', kind='vacancy', experience=experience)
                self.assertTrue(accepted, reason)
                self.assertEqual(it.categories, ['internships'])

    def test_senior_or_experienced_vacancies(self):
        for title, body, experience in [
            ('Senior Python Developer', 'Помогаем студентам', '3-6 лет'),
            ('Ведущий инженер Linux', 'Есть наставник для стажеров', ''),
            ('Junior Python Developer', 'Опыт работы от 3 лет обязателен', '0-1 год'),
            ('Python Developer', 'Работа над продуктом', ''),
        ]:
            with self.subTest(title=title):
                self.assertFalse(self.decide(title, body, kind='vacancy', experience=experience)[0])

    def test_non_technical_beginner_vacancies(self):
        for title in ['Стажёр в отдел кадров', 'Стажёр по ведению финансового документооборота',
                      'Стажёр-менеджер по продажам IT-решений']:
            with self.subTest(title=title):
                self.assertFalse(self.decide(title, 'В аккредитованной IT-компании', kind='vacancy')[0])

    def test_student_mentions_in_company_benefits_do_not_prove_entry_level(self):
        self.assertFalse(self.decide('Python Developer', 'Наши сотрудники проводят семинары для студентов', kind='vacancy')[0])
        self.assertTrue(self.decide('Вакансия Python для студентов', 'Работа над сервером', kind='vacancy')[0])

    def test_senior_speaker_does_not_exclude_event(self):
        accepted, reason, _ = self.decide('Приглашаем на митап Python для начинающих', 'Спикер — senior-разработчик, руководитель команды')
        self.assertTrue(accepted, reason)

    def test_experienced_mentor_does_not_make_a_junior_job_senior(self):
        accepted, reason, _ = self.decide('Junior Python Developer', 'Поможет наставник с опытом работы 5 лет', kind='vacancy')
        self.assertTrue(accepted, reason)

    def test_experienced_only_audience_is_excluded(self):
        self.assertFalse(self.decide('Приглашаем на конференцию по Python', 'Только для опытных руководителей')[0])

    def test_structured_closed_flag_wins(self):
        accepted, _, it = self.decide('Приглашаем на стажировку Python', 'Регистрация открыта. Заявки до 15 ноября', explicitly_closed=True)
        self.assertFalse(accepted)
        self.assertEqual(it.registration_status,'closed')

    def test_structured_deadline_overrides_text(self):
        accepted, _, _ = self.decide('Приглашаем на митап Python', 'Заявки до 15 ноября', deadline=NOW-timedelta(days=1))
        self.assertFalse(accepted)

    def test_stale_publication_is_rejected(self):
        self.assertFalse(self.decide('Приглашаем на митап Python', published_at=NOW-timedelta(days=61))[0])

    def test_multiple_deadlines_are_unknown_but_all_expired_are_rejected(self):
        accepted, _, it = self.decide('Приглашаем на митап Python', 'Заявки до 15 октября; заявки до 15 ноября')
        self.assertTrue(accepted)
        self.assertIsNone(it.deadline)
        self.assertEqual(it.registration_status,'unknown')
        accepted, _, _ = self.decide('Приглашаем на митап Python', 'Заявки до 15 октября; заявки до 20 октября')
        self.assertFalse(accepted)

    def test_passed_event_without_application_deadline(self):
        self.assertFalse(self.decide('Приглашаем на митап Python', 'Митап состоится 20 октября')[0])
        self.assertTrue(self.decide('Митап Python', 'Митап состоится 25 октября')[0])

    def test_past_dates_in_ctf_invitations_and_schedule_lines(self):
        cases = [
            ('Пермь, ваш выход!', '10 октября приходите на PERM CTF 2026. Зарегистрируйтесь!'),
            ('Возьми телефон', 'Приходите 1 октября — принять участие в CTF.'),
            ('Пентест инфраструктуры', 'Приглашаем на открытый урок.\n🗓 21 октября в 20:00 МСК'),
            ('Митап Python', 'Приглашаем!\nДата: 21 октября'),
            ('Семинар по SQL', 'Приглашаем!\n📆 21 октября'),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                accepted, reason, _ = self.decide(title, body)
                self.assertFalse(accepted, reason)
                self.assertEqual(reason, 'дата мероприятия уже прошла')

    def test_schedule_dates_keep_future_events_and_ranges(self):
        for body in ['Приглашаем!\n🗓 23 октября в 20:00',
                     'Дата: 23 октября', 'Приходите 23 октября',
                     'Сроки проведения: с 20 по 23 октября',
                     'Сроки проведения: 20–23 октября']:
            with self.subTest(body=body):
                self.assertTrue(self.decide('Митап Python', body)[0])

    def test_unrelated_date_on_new_line_is_not_event_evidence(self):
        self.assertTrue(self.decide('Приглашаем на митап Python',
                                   'Компания основана\n21 октября 2000 года')[0])

    def test_keywords_can_extend_or_disable_categories(self):
        self.rules.categories = {'events': {'label':'События', 'keywords':['техзавтрак']}}
        accepted, reason, it = self.decide('Приглашаем на техзавтрак по SQL')
        self.assertTrue(accepted,reason)
        self.assertEqual(it.categories,['events'])
        self.rules.categories = {}
        self.assertFalse(self.decide('Приглашаем на митап Python')[0])

    def test_custom_exclusion_and_score(self):
        self.rules.exclude_keywords.append('платный')
        self.assertFalse(self.decide('Приглашаем на платный митап Python')[0])
        self.rules.min_score = 100
        self.assertFalse(self.decide('Приглашаем на митап Python')[0])

    def test_it_context_can_be_disabled_explicitly(self):
        self.rules.require_it_context = False
        self.assertTrue(self.decide('Приглашаем на день карьеры', 'Встреча студентов и работодателей')[0])

    def test_old_categories_are_cleared_when_rejected(self):
        it = item('Конкурс котиков');it.categories=['events']
        self.assertFalse(self.filters.check(it,now=NOW)[0])
        self.assertEqual(it.categories,[])


class DeadlineTests(unittest.TestCase):
    def parse(self, text, published=NOW):
        return parse_deadline(text,published,now=NOW)

    def test_same_day_and_past_date_do_not_roll_year(self):
        self.assertEqual(self.parse('Заявки до 22 октября').year,2026)
        self.assertEqual(self.parse('Заявки до 15 октября').year,2026)
        self.assertLess(self.parse('Заявки до 15 октября'),NOW)
        self.assertGreater(self.parse('Заявки до 22 октября'),NOW)

    def test_numeric_iso_month_and_explicit_year(self):
        for value in ['15.11.2026','15/11/26','2026-11-15','15 ноября 2026','15.11']:
            with self.subTest(value=value):
                self.assertEqual(self.parse('Заявки до '+value).isoformat(),'2026-11-15T23:59:59.999999+03:00')

    def test_explicit_year_is_respected(self):
        self.assertEqual(self.parse('Заявки до 15 ноября 2027').year,2027)
        self.assertEqual(self.parse('Заявки до 15 ноября',datetime(2025,12,1,tzinfo=ZoneInfo('Europe/Moscow'))).year,2025)

    def test_numeric_year_suffix_is_not_dropped(self):
        self.assertEqual(self.parse('Заявки до 15.11.2027г.').year,2027)
        self.assertIsNone(self.parse('Заявки до 15.11.20277'))

    def test_dates_joined_by_or_are_not_arbitrarily_selected(self):
        self.assertIsNone(self.parse('Регистрация на митап до 15 ноября или 20 ноября'))
        self.assertIsNone(self.parse('Заявки до 15.11.2026 или 20.11.2026'))

    def test_relative_dates_anchor_to_publication_not_scan(self):
        published=datetime(2026,10,15,10,tzinfo=ZoneInfo('Europe/Moscow'))
        for word,day in [('сегодня',15),('завтра',16),('послезавтра',17)]:
            self.assertEqual(self.parse('Заявки до '+word,published).day,day)
        self.assertIsNone(self.parse('Заявки до завтра',None))

    def test_clock_and_moscow_end_of_day(self):
        self.assertEqual(self.parse('Заявки до 22 октября в 14:30').hour,14)
        deadline=self.parse('Заявки до 22 октября')
        rules=Config.load(str(ROOT/'config.yaml')).rules
        self.assertEqual(detect_status('Регистрация открыта',deadline,rules,now=datetime.fromisoformat('2026-10-22T20:59:59+00:00')),'open')
        self.assertEqual(detect_status('Регистрация открыта',deadline,rules,now=datetime.fromisoformat('2026-10-22T21:00:00+00:00')),'closed')

    def test_invalid_dates_and_unrelated_dates_stay_unknown(self):
        for value in ['Заявки до 31 февраля','Заявки до следующей пятницы',
                      'Компания основана 15 ноября 2000 года','Опубликовано 22.10.2026','Оклад до 15.11 рублей']:
            self.assertIsNone(self.parse(value))

    def test_unknown_year_uses_current_year_if_no_publication(self):
        self.assertEqual(self.parse('Заявки до 15 ноября',None).year,2026)

    def test_software_versions_are_not_event_dates(self):
        now = datetime(2026,12,10,12,tzinfo=ZoneInfo('Europe/Moscow'))
        rules=Config.load(str(ROOT/'config.yaml')).rules
        for title in ['Приглашаем на митап Python 3.12', 'Приглашаем на вебинар Python3.12']:
            it=item(title,published_at=now)
            accepted, reason=Filters(rules).check(it,now=now)
            self.assertTrue(accepted,reason)

    def test_recheck_does_not_extend_a_deadline(self):
        rules=Config.load(str(ROOT/'config.yaml')).rules
        it=item('Приглашаем на митап Python','Заявки до 22 октября')
        filters=Filters(rules)
        self.assertTrue(filters.check(it,now=NOW)[0])
        self.assertFalse(filters.check(it,now=NOW+timedelta(days=1))[0])
        self.assertEqual(it.deadline.year,2026)

    def test_deadline_notification_displays_clock_and_timezone(self):
        cfg=Config.load(str(ROOT/'config.yaml'));bot=Bot.__new__(Bot);bot.cfg=cfg
        it=item('Приглашаем на митап Python','Регистрация открыта. Заявки до 22 октября в 14:30')
        self.assertTrue(Filters(cfg.rules).check(it,now=NOW)[0])
        message=bot.format_message(it)
        self.assertIn('2026-10-22 14:30 (Europe/Moscow)',message)


class RealPublicationTests(unittest.TestCase):
    def test_manually_labelled_public_posts(self):
        # Labels refer to the publication content at NOW, not the classifier's output.
        cases={'mgtsstudents':{'16':True},
               'rostelecom_tech':{'1891':False,'1903':False},
               'rostelecom_molodezh':{'889':False,'891':False,'895':False},
               'brainzbycroc':{'2179':False,'2194':True},
               'CTDLru':{'670':False,'685':False},
               'rvision_team':{'1989':True}}
        filters=Filters(Config.load(str(ROOT/'config.yaml')).rules)
        for channel,labels in cases.items():
            parsed,_=_parse_page((ROOT/'tests/fixtures/telegram'/f'{channel}.html').read_text(),channel)
            posts={i.external_id:i for i in parsed}
            for identifier,expected in labels.items():
                with self.subTest(post=channel+'/'+identifier):
                    accepted,reason=filters.check(posts[identifier],now=NOW)
                    self.assertEqual(accepted,expected,reason)
        self.assertEqual(sum(map(len,cases.values())),11)

    def test_real_k2_and_croc_soc_internships(self):
        filters=Filters(Config.load(str(ROOT/'config.yaml')).rules)
        for filename in ['k2_detail.html','croc_detail.html']:
            soup=BeautifulSoup((ROOT/'tests/fixtures/websites'/filename).read_text(),'html.parser')
            if filename.startswith('k2'):
                nodes=[n for n in soup.select('.t-rec') if n.get('data-record-type') in {'1060','491','106','795','508','1095'}]
            else:
                nodes=soup.select('.vacancy-detail__content-main-text-block, .vacancy-detail__content-main-item')
            it=item('Стажер-аналитик SOC','\n'.join(text(n) for n in nodes),kind='vacancy',published_at=None)
            accepted,reason=filters.check(it,now=NOW)
            self.assertTrue(accepted,reason)
            self.assertEqual(it.registration_status,'unknown')


class RuleConfigurationTests(unittest.TestCase):
    def test_invalid_settings_fail_explicitly(self):
        base=yaml.safe_load((ROOT/'config.yaml').read_text())
        cases=[('timezone','not/a/timezone'),('require_it_context','false'),('min_score',0),
               ('max_age_days',-1),('categories',[]),('categories',{'typo':{}}),
               ('exclude_keywords','not a list'),('closed_words',[42])]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml'
            for name,value in cases:
                with self.subTest(name=name):
                    raw=copy.deepcopy(base);raw['rules'][name]=value;path.write_text(yaml.safe_dump(raw))
                    with self.assertRaises(ConfigurationError):Config.load(str(path))


if __name__=='__main__':
    unittest.main()
