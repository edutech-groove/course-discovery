import logging

from django.core.management import BaseCommand, CommandError
from django.db import transaction
from django.utils.translation import ugettext as _

from course_discovery.apps.course_metadata.models import DeletePersonDupsConfig, Endorsement, Partner, Person

logger = logging.getLogger(__name__)


class PersonInfo(object):
    def __init__(self, partner, uuid, target_uuid):
        self.person = Person.objects.get(partner=partner, uuid=uuid)
        self.target = Person.objects.get(partner=partner, uuid=target_uuid)

        if self.person.profile_url:
            raise CommandError('Cannot delete person {} - they have a marketing profile url'.format(uuid))


class Command(BaseCommand):
    help = _('Delete duplicate persons in course_metadata.')

    def add_arguments(self, parser):
        parser.add_argument(
            'people',
            metavar=_('PERSON'),
            nargs='*',
            help=_('People to delete, in UUID:TARGET_UUID form.'),
        )
        parser.add_argument(
            '--partner-code',
            metavar=_('PARTNER'),
            help=_('Short code for a partner.'),
        )
        parser.add_argument(
            '--commit',
            action='store_true',
            help=_('Actually commit the changes to the database.'),
        )
        parser.add_argument(
            '--args-from-database',
            action='store_true',
            help=_('Use arguments from the DeletePersonDupsConfig model instead of the command line.'),
        )

    def get_args_from_database(self):
        """ Returns an options dictionary from the current DeletePersonDupsConfig model. """
        config = DeletePersonDupsConfig.get_solo()

        # We don't need fancy shell-style whitespace/quote handling - none of our arguments are complicated
        argv = config.arguments.split()

        parser = self.create_parser('manage.py', 'delete_person_dups')
        return parser.parse_args(argv).__dict__   # we want a dictionary, not a non-iterable Namespace object

    def parse_person(self, partner, person_str):
        person_parts = person_str.split(':')

        if len(person_parts) != 2:
            raise CommandError(_('Malformed argument "{}", should be in form of UUID:TARGET_UUID').format(person_str))
        if person_parts[0] == person_parts[1]:
            raise CommandError(_('Malformed argument "{}", UUIDs cannot be equal').format(person_str))

        return PersonInfo(partner, person_parts[0], person_parts[1])

    # Wrap the action in an atomic transaction to be super cautious here.
    # If anything goes wrong while we are fixing up foreign key references, we want to roll it all back.
    # Because of this (and because this tool doesn't need to be pretty), we don't catch any exceptions.
    @transaction.atomic
    def delete_person(self, pinfo, commit=False):
        # Foreign keys to worry about:
        #
        # Just delete
        # - Position
        # - PersonSocialNetwork
        # - PersonWork
        #
        # Move to target
        # - Endorsement
        # - Program instructor_ordering (sortedm2m)
        # - CourseRun staff (sortedm2m)
        # - Publisher CourseRun staff (sortedm2m)

        logger.info(
            '{} {}:\n'.format(_('Deleting') if commit else _('Would delete'), pinfo.person.uuid) +
            ' {}: {}\n'.format(_('Name'), pinfo.person.full_name) +
            ' {}: {}\n'.format(_('Endorsements'), pinfo.person.endorsement_set.count()) +
            ' {}: {}\n'.format(_('Programs'), pinfo.person.program_set.count()) +
            ' {}: {}\n'.format(_('Course Runs'), pinfo.person.courses_staffed.count()) +
            ' {}: {}\n'.format(_('Publisher Course Runs'), pinfo.person.publisher_course_runs_staffed.count()) +
            ' {}: {} ({})\n'.format(_('Target'), pinfo.target.full_name, pinfo.target.uuid)
        )
        if not commit:
            return

        # Move endorsements
        Endorsement.objects.filter(endorser=pinfo.person).update(endorser=pinfo.target)

        def filter_person(person):
            return pinfo.target if person == pinfo.person else person

        # Update programs
        for program in pinfo.person.program_set.all():
            if pinfo.target in program.instructor_ordering.all():
                continue
            new_instructors = [filter_person(instructor) for instructor in program.instructor_ordering.all()]
            program.instructor_ordering = new_instructors
            program.save()

        # Update metadata course runs
        for course_run in pinfo.person.courses_staffed.all():
            if pinfo.target in course_run.staff.all():
                continue
            new_staff = [filter_person(staff) for staff in course_run.staff.all()]
            course_run.staff = new_staff
            course_run.save()

        # Update publisher course runs
        for publisher_course_run in pinfo.person.publisher_course_runs_staffed.all():
            if pinfo.target in publisher_course_run.staff.all():
                continue
            new_staff = [filter_person(staff) for staff in publisher_course_run.staff.all()]
            publisher_course_run.staff = new_staff
            publisher_course_run.save()

        # And finally, actually delete the person
        pinfo.person.delete()

    def delete_person_dups(self, options):
        if options['partner_code'] is None:
            self.print_help('manage.py', 'delete_person_dups')
            raise CommandError(_('You must specify --partner-code'))
        if len(options['people']) == 0:
            self.print_help('manage.py', 'delete_person_dups')
            raise CommandError(_('You must specify at least one person'))

        partner = Partner.objects.get(short_code=options['partner_code'])
        pinfos = [self.parse_person(partner, p) for p in options['people']]
        commit = options['commit']

        for info in pinfos:
            self.delete_person(info, commit=commit)

    def handle(self, *args, **options):
        if options['args_from_database']:
            options = self.get_args_from_database()

        self.delete_person_dups(options)