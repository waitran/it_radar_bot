"""Fixed-clock labelled publications plus independent scope/compatibility checks."""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from app.config import Config
from app.filters import Filters
from app.formatting import format_message
from app.models import Item
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 12, tzinfo=ZoneInfo('Europe/Moscow'))

CORPUS = json.loads((ROOT/'tests/fixtures/filtering/publications.json').read_text())


def publication(title, text='', **kwargs):
    return Item('test', 'Offline', '1', '', title, '', None,
                kwargs.pop('published_at', NOW), raw_text=text, **kwargs)


class CorpusTests(unittest.TestCase):
    pass


def corpus_test(row):
    def test(self):
        item = publication(row['title'], row['text'])
        actual, reason = Filters(Config.load(str(ROOT/'config.yaml')).rules).check(item, now=NOW)
        self.assertEqual(actual, row['expected'], reason)
    return test


for row in CORPUS:
    setattr(CorpusTests, 'test_' + row['id'], corpus_test(row))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.rules = Config.load(str(ROOT/'config.yaml')).rules
        self.selector = Filters(self.rules)

    def explain(self, title, text='', **kwargs):
        return self.selector.explain(publication(title, text, **kwargs), now=NOW)

    def test_corpus_balance_and_full_texts(self):
        self.assertEqual(len({r['id'] for r in CORPUS}), 50)
        self.assertEqual(sum(r['expected'] for r in CORPUS), 25)
        self.assertTrue(all(len(r['text']) > 100 for r in CORPUS))

    def test_both_reported_reasons(self):
        for identifier, code in [('donation', 'non_it_context'), ('robot_news', 'informational_only')]:
            row = next(r for r in CORPUS if r['id'] == identifier)
            decision = self.explain(row['title'], row['text'])
            self.assertFalse(decision['accepted'])
            self.assertEqual(decision['reason'], code)

    def test_same_technical_word_news_and_public_invitation(self):
        title = 'Чемпионат по робототехнике'
        news = 'Команды университета выступят в финале 31 октября 2026 года. Состав участников определен.'
        self.assertFalse(self.explain(title, news)['accepted'])
        self.assertTrue(self.explain(title, news + ' Приглашаем зрителей, регистрация по ссылке.')['accepted'])

    def test_ambiguous_words_alone_do_not_prove_it(self):
        for word in ['тех', 'it', 'ит', 'go', 'tech', 'data', 'cloud', 'иб', 'ии', 'цифровой', 'технологии']:
            with self.subTest(word=word):
                self.assertFalse(self.explain('Открытый конкурс для студентов',
                    f'Приглашаем всех желающих. Название проекта — {word}. Регистрация открыта.')['accepted'])

    def test_technical_collocations_prove_it(self):
        for subject in ['язык Go', 'Data Science', 'Cloud computing', 'информационные технологии',
                        'разработка программного обеспечения', 'техническая конференция']:
            with self.subTest(subject=subject):
                self.assertTrue(self.explain('Приглашаем на семинар',
                    f'Тема семинара — {subject}. Регистрация открыта для всех желающих.')['accepted'])

    def test_unrelated_url_alone_is_not_action(self):
        item = publication('Конференция по Python', 'Делегация университета выедет 5 ноября 2026 года.')
        item.apply_url = 'https://example.org/register'
        self.assertFalse(self.selector.check(item, now=NOW)[0])

    def test_url_path_is_not_it_evidence(self):
        self.assertFalse(self.explain('Конкурс для студентов',
            'Приглашаем участвовать. Регистрация открыта: https://example.org/python/register')['accepted'])

    def test_non_it_action_cannot_inherit_robotics(self):
        self.assertFalse(self.explain('Чемпионат по робототехнике',
            'Команды выступят 31 октября. Зарегистрируйтесь для донации в Службе крови.')['accepted'])

    def test_non_it_context_does_not_veto_independent_it_invitation(self):
        result = self.explain('Донорский клуб поддерживает студентов',
            'Университет помогает донорам. Приглашаем на открытый Python-воркшоп 5 ноября 2026 года. '
            'Регистрация на воркшоп по ссылке, участие бесплатное.')
        self.assertTrue(result['accepted'], result)

    def test_charitable_technical_project_is_kept(self):
        self.assertTrue(self.explain('Благотворительный хакатон по Python',
            'Приглашаем разработчиков создать сервис для помощи донорам. Регистрация открыта.')['accepted'])

    def test_application_period_is_an_explicit_action(self):
        for body in ['Заявки принимаются до 5 ноября 2026 года.', 'Регистрация до 5 ноября 2026 года.']:
            self.assertTrue(self.explain('Хакатон по Python', body)['accepted'])

    def test_sponsor_technology_does_not_make_a_photo_contest_technical(self):
        decision = self.explain('Конкурс фотографий кампуса',
            'Спонсор конкурса — сообщество Python. Регистрация открыта для всех желающих.')
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['reason'], 'irrelevant_competition')

    def test_different_registration_target_does_not_inherit_it(self):
        self.assertFalse(self.explain('Чемпионат по робототехнике',
            'Регистрация на экскурсию в музей открыта. Наши команды выступят в финале 31 октября.')['accepted'])

    def test_display_prefers_heading_to_incidental_hackathon(self):
        item = publication('Конференция Python',
            'Приглашаем на конференцию. Спикер ранее выиграл хакатон по Python.')
        item.categories = ['events']
        self.assertIn('Тип: Конференция', format_message(item, self.rules))

    def test_negated_and_third_party_actions(self):
        for body in ['Не регистрируйтесь, информация об условиях появится позже.',
                     'Команды смогут зарегистрироваться только по персональному приглашению.',
                     'Студенты успели подать заявку на финал.',
                     'Рассказываем, как зарегистрироваться в системе учета результатов.',
                     'Регистрация была открыта в начале месяца. Следующая конференция 5 ноября.']:
            with self.subTest(body=body):
                self.assertFalse(self.explain('Конференция Python', body)['accepted'])

    def test_general_student_eligibility_is_an_opportunity(self):
        self.assertTrue(self.explain('Хакатон по Python',
            'Студенты могут подать заявку до 5 ноября 2026 года. Участие бесплатное.')['accepted'])

    def test_recap_future_date_does_not_reopen_event(self):
        decision = self.explain('Хакатон Python прошел в кампусе',
            'Регистрация была открыта для студентов. Следующий финал состоится 15 ноября 2026 года.')
        self.assertEqual(decision['reason'], 'expired_event')
        self.assertEqual(decision['publication_type'], 'retrospective')

    def test_new_invitation_after_recap_is_kept(self):
        decision = self.explain('Митап по Python прошел в кампусе',
            'Приходите на следующий митап 5 ноября 2026 года, регистрация открыта.')
        self.assertTrue(decision['accepted'])

    def test_reason_codes_for_dates_status_and_missing_category(self):
        cases = [
            ('Приглашаем на митап Python', 'Митап состоится 20 октября 2026 года.', 'expired_event'),
            ('Приглашаем на митап Python', 'Регистрация закрыта.', 'closed_registration'),
            ('Приглашаем на митап Python', 'Заявки до 20 октября 2026 года.', 'expired_event'),
            ('Приглашаем на митап Python', 'Регистрация пока не открыта.', 'closed_registration'),
            ('Python в повседневной работе', 'Разбираем полезные инструменты.', 'insufficient_evidence'),
            ('Митап Python', 'Митап состоится 5 ноября 2026 года.', 'no_user_action'),
        ]
        for title, text, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.explain(title, text)['reason'], code)
        self.assertEqual(self.explain('Приглашаем на митап Python', published_at=NOW-timedelta(days=61))['reason'], 'expired_event')

    def test_types_do_not_change_storage_schema(self):
        for identifier, expected in [('python_hackathon', 'actionable_competition'),
                                     ('devops', 'actionable_event'), ('backend_intern', 'internship'),
                                     ('devsecops_job', 'vacancy'), ('linux', 'educational_opportunity'),
                                     ('robot_news', 'informational_news'), ('sport', 'irrelevant')]:
            row = next(r for r in CORPUS if r['id'] == identifier)
            self.assertEqual(self.explain(row['title'], row['text'])['publication_type'], expected)

    def test_explain_does_not_mutate_item(self):
        item = publication('Приглашаем на митап Python', 'Регистрация открыта. Заявки до 5 ноября.')
        before = copy.deepcopy(item)
        self.selector.explain(item, now=NOW)
        self.assertEqual(item, before)
        self.assertEqual(item.content_hash, before.content_hash)

    def test_category_ids_and_personal_settings_remain_compatible(self):
        self.assertEqual(set(self.rules.categories), {'events', 'contests', 'internships', 'career'})
        self.rules.require_it_context = False
        self.assertTrue(self.explain('Приглашаем на конференцию библиотекарей')['accepted'])
        self.assertTrue(self.explain('Конкурс фотографий', 'Регистрация открыта для всех желающих.')['accepted'])
        self.rules.categories = {}
        self.assertFalse(self.explain('Приглашаем на митап Python')['accepted'])

    def test_robot_display_never_claims_hackathon(self):
        row = next(r for r in CORPUS if r['id'] == 'robot_viewers')
        item = publication(row['title'], row['text'])
        self.assertTrue(self.selector.check(item, now=NOW)[0])
        message = format_message(item, self.rules)
        self.assertIn('Категория: Соревнования', message)
        self.assertIn('Тип: Чемпионат по робототехнике', message)
        self.assertNotIn('Хакатон', message)


if __name__ == '__main__':
    unittest.main()
