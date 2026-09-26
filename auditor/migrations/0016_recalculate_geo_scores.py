"""Przelicza wyniki badań GEO według ujednoliconej punktacji.

Do tej pory `citation_rate` i `overall_score` liczyły wyłącznie cytowania z linkiem.
Po ujednoliceniu jedna próba daje jeden punkt także za samą nazwę marki, więc badania
zapisane wcześniej pokazywałyby w karcie KPI inną liczbę niż tabela porównawcza
(liczona na żywo z prób). Ta migracja wyrównuje dane historyczne.
"""
from django.db import migrations


def recalculate(apps, schema_editor):
    GeoStudy = apps.get_model("auditor", "GeoStudy")

    for study in GeoStudy.objects.prefetch_related("queries__runs"):
        visible_total = usable_total = 0

        for query in study.queries.all():
            runs = [run for run in query.runs.all() if not run.error]
            visible = [run for run in runs if run.brand_cited or run.brand_mentioned]

            query.citation_rate = round(len(visible) / len(runs) * 100) if runs else 0
            query.save(update_fields=["citation_rate"])

            usable_total += len(runs)
            visible_total += len(visible)

        study.overall_score = (
            round(visible_total * 100 / usable_total) if usable_total else 0
        )
        study.save(update_fields=["overall_score"])


def noop(apps, schema_editor):
    """Wstecz nie ma czego odtwarzać - stara wartość nie była nigdzie zachowana."""


class Migration(migrations.Migration):

    dependencies = [
        ("auditor", "0015_geostudy_competitors_input"),
    ]

    operations = [
        migrations.RunPython(recalculate, noop),
    ]
