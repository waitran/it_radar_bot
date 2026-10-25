import {
  ArrowUpRight,
  BadgeCheck,
  Bot,
  Globe2,
  LockKeyhole,
  Radar,
} from "lucide-react";
import { botUrl } from "../telegram";
import type { User } from "../types";
import { ExternalLink } from "../components/common";

export function Profile({ user }: { user: User }) {
  const bot = botUrl();
  return (
    <div className="screen profile-screen">
      <div className="page-heading">
        <p className="eyebrow">ТВОЙ IT RADAR</p>
        <h1>
          Профиль<span className="heading-dot">.</span>
        </h1>
        <p>Все возможности начинаются с тебя.</p>
      </div>
      <section className="profile-card">
        <div className="avatar">
          {user.first_name.slice(0, 1).toUpperCase()}
          {user.last_name?.slice(0, 1).toUpperCase()}
        </div>
        <h2>
          {user.first_name} {user.last_name}
        </h2>
        {user.username && <p className="muted">@{user.username}</p>}
        <span className="verified-label">
          <BadgeCheck size={15} />
          Подтверждено Telegram
        </span>
        <dl className="profile-fields">
          <div>
            <dt>Telegram ID</dt>
            <dd>{user.id}</dd>
          </div>
          {user.language_code && (
            <div>
              <dt>
                <Globe2 size={14} />
                Язык Telegram
              </dt>
              <dd>{user.language_code}</dd>
            </div>
          )}
        </dl>
      </section>
      <section className="about-card">
        <span className="about-icon">
          <Radar size={25} />
        </span>
        <p className="eyebrow">ВОЗМОЖНОСТИ РЯДОМ</p>
        <h2>
          Помогаем найти
          <br />
          свой путь в IT.
        </h2>
        <p>
          IT Radar собирает стажировки, мероприятия и конкурсы из открытых
          источников. Меньше времени на поиск — больше на то, что тебе
          интересно.
        </p>
        <div className="about-version">
          IT Radar Mini App<span>v0.4</span>
        </div>
      </section>
      {bot ? (
        <ExternalLink href={bot} className="bot-link">
          <span className="bot-icon">
            <Bot size={23} />
          </span>
          <span>
            <strong>IT Radar в Telegram</strong>
            <small>Перейти к боту</small>
          </span>
          <ArrowUpRight size={20} />
        </ExternalLink>
      ) : (
        <p className="config-note">
          <Bot size={18} />
          Ссылка на бота появится после настройки его username.
        </p>
      )}
      <p className="privacy-note">
        <LockKeyhole size={15} />
        Данные профиля получены из проверенной Telegram-сессии. Токен бота
        остаётся на сервере.
      </p>
    </div>
  );
}
