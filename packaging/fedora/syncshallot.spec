Name:           syncshallot
Version:        %{ss_version}
Release:        1%{?dist}
Summary:        De gemakkelijkste manier om je backup schijven te synchroniseren
License:        GPL-3.0-or-later
URL:            https://github.com/rene-timmermans/SyncShallot
Source0:        syncshallot-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3
Requires:       python3 >= 3.11
Requires:       python3-pyside6 >= 6.6
Requires:       qt6-qtbase-gui
Recommends:     qt6-qtwayland
Recommends:     qt6-qttranslations
%global __brp_python_bytecompile %{nil}
%global __requires_exclude_from ^%{_datadir}/syncshallot/.*$

%description
Vergelijk bron en back-up inclusief submappen, kopieer nieuwe en gewijzigde
bestanden en bekijk welke verouderde bestanden alleen op de back-up staan.
Kies zelf of je die laat staan, isoleert of verwijdert. De bron blijft
standaard beschermd. Voor lokale mappen en gemounte USB- en netwerkdrives.

%prep
%setup -q

%install
sh packaging/stage.sh %{buildroot}

%files
%{_bindir}/syncshallot
%{_datadir}/syncshallot/
%{_datadir}/applications/syncshallot.desktop
%{_datadir}/icons/hicolor/256x256/apps/syncshallot.png
%{_docdir}/syncshallot/
%license %{_datadir}/licenses/syncshallot/LICENSE
