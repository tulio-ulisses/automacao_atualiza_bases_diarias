create or replace function public.preparar_carga_iilex(
    p_tabela text,
    p_colunas text[]
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    coluna text;
begin
    if p_tabela not in (
        'contencioso',
        'peticoes_loy',
        'historico',
        'historico_120',
        'agenda'
    ) then
        raise exception 'Tabela não autorizada: %', p_tabela;
    end if;

    if coalesce(array_length(p_colunas, 1), 0) = 0 then
        raise exception 'Nenhuma coluna informada';
    end if;

    execute format(
        'create table if not exists public.%I (__temporaria__ text)',
        p_tabela
    );

    foreach coluna in array p_colunas
    loop
        if coluna !~ '^[A-Za-z_][A-Za-z0-9_]*$' then
            raise exception 'Nome de coluna inválido: %', coluna;
        end if;

        if not exists (
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = p_tabela
              and column_name = coluna
        ) then
            execute format(
                'alter table public.%I add column %I text',
                p_tabela,
                coluna
            );
        end if;
    end loop;

    if exists (
        select 1
        from information_schema.columns
        where table_schema = 'public'
          and table_name = p_tabela
          and column_name = '__temporaria__'
    ) then
        execute format(
            'alter table public.%I drop column "__temporaria__"',
            p_tabela
        );
    end if;

    execute format(
        'truncate table public.%I',
        p_tabela
    );

    execute format(
        'grant select, insert, update, delete on table public.%I to service_role',
        p_tabela
    );

    perform pg_notify('pgrst', 'reload schema');
end;
$$;

revoke all
on function public.preparar_carga_iilex(text, text[])
from public;

revoke all
on function public.preparar_carga_iilex(text, text[])
from anon;

revoke all
on function public.preparar_carga_iilex(text, text[])
from authenticated;

grant execute
on function public.preparar_carga_iilex(text, text[])
to service_role;
