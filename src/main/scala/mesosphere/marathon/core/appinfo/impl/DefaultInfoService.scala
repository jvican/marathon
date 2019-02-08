package mesosphere.marathon
package core.appinfo.impl

import akka.stream.Materializer
import akka.stream.scaladsl.{Sink, Source}
import com.typesafe.scalalogging.StrictLogging
import mesosphere.marathon.core.appinfo.AppInfo.Embed
import mesosphere.marathon.core.appinfo._
import mesosphere.marathon.core.group.GroupManager
import mesosphere.marathon.core.pod.PodDefinition
import mesosphere.marathon.core.storage.repository.RepositoryConstants
import mesosphere.marathon.raml.PodStatus
import mesosphere.marathon.state._
import mesosphere.marathon.stream.Implicits._

import scala.async.Async.{async, await}
import scala.collection.immutable.Seq
import scala.collection.mutable
import scala.concurrent.{ExecutionContext, Future}

private[appinfo] class DefaultInfoService(
    groupManager: GroupManager,
    newBaseData: () => AppInfoBaseData)(implicit ec: ExecutionContext)
  extends AppInfoService with GroupInfoService with PodStatusService with StrictLogging {

  override def selectPodStatus(id: PathId, selector: PodSelector): Future[Option[PodStatus]] =
    async { // linter:ignore UnnecessaryElseBranch
      logger.debug(s"query for pod $id")
      val maybePod = groupManager.pod(id)
      maybePod.filter(selector.matches) match {
        case Some(pod) => Some(await(newBaseData().podStatus(pod)))
        case None => Option.empty[PodStatus]
      }
    }

  override def selectPodStatuses(ids: Set[PathId], selector: PodSelector)(implicit materializer: Materializer): Future[Seq[PodStatus]] = {
    val baseData = newBaseData()

    val pods = ids.flatMap(groupManager.pod(_)).toVector
    Future.sequence(pods.map(baseData.podStatus(_)))
  }

  override def selectApp(id: PathId, selector: AppSelector, embed: Set[AppInfo.Embed]): Future[Option[AppInfo]] = {
    logger.debug(s"queryForAppId $id")
    groupManager.app(id) match {
      case Some(app) if selector.matches(app) => newBaseData().appInfoFuture(app, embed).map(Some(_))
      case None => Future.successful(None)
    }
  }

  override def selectAppsBy(selector: AppSelector, embed: Set[AppInfo.Embed]): Future[Seq[AppInfo]] =
    async { // linter:ignore UnnecessaryElseBranch
      logger.debug("queryAll")
      val rootGroup = groupManager.rootGroup()
      val selectedApps: IndexedSeq[AppDefinition] = rootGroup.transitiveApps.filterAs(selector.matches)(collection.breakOut)
      val infos = await(resolveAppInfos(selectedApps, embed))
      infos
    }

  override def selectAppsInGroup(groupId: PathId, selector: AppSelector,
    embed: Set[AppInfo.Embed]): Future[Seq[AppInfo]] =

    async { // linter:ignore UnnecessaryElseBranch
      logger.debug(s"queryAllInGroup $groupId")
      val maybeGroup: Option[Group] = groupManager.group(groupId)
      val maybeApps: Option[IndexedSeq[AppDefinition]] =
        maybeGroup.map(_.transitiveApps.filterAs(selector.matches)(collection.breakOut))
      maybeApps match {
        case Some(selectedApps) => await(resolveAppInfos(selectedApps, embed))
        case None => Seq.empty
      }
    }

  override def selectGroup(groupId: PathId, selectors: GroupInfoService.Selectors,
    appEmbed: Set[Embed], groupEmbed: Set[GroupInfo.Embed]): Future[Option[GroupInfo]] = {
    groupManager.group(groupId) match {
      case Some(group) => queryForGroup(group, selectors, appEmbed, groupEmbed)
      case None => Future.successful(None)
    }
  }

  override def selectGroupVersion(groupId: PathId, version: Timestamp, selectors: GroupInfoService.Selectors,
    groupEmbed: Set[GroupInfo.Embed]): Future[Option[GroupInfo]] = {
    groupManager.group(groupId, version).flatMap {
      case Some(group) => queryForGroup(group, selectors, Set.empty, groupEmbed)
      case None => Future.successful(None)
    }
  }

  private case class LazyCell[T](evalution: () => T) { lazy val value = evalution() }

  private[this] def queryForGroup(
    group: Group,
    selectors: GroupInfoService.Selectors,
    appEmbed: Set[AppInfo.Embed],
    groupEmbed: Set[GroupInfo.Embed]): Future[Option[GroupInfo]] =

    async { // linter:ignore UnnecessaryElseBranch
      val cachedBaseData = LazyCell(() => newBaseData()) // Work around strange async/eval compile bug in Scala 2.12

      val groupEmbedApps = groupEmbed(GroupInfo.Embed.Apps)
      val groupEmbedPods = groupEmbed(GroupInfo.Embed.Pods)

      //fetch all transitive app infos and pod statuses with one request
      val infoById: Map[PathId, AppInfo] =
        if (groupEmbedApps) {
          val filteredApps: IndexedSeq[AppDefinition] =
            group.transitiveApps.filterAs(selectors.appSelector.matches)(collection.breakOut)
          await(resolveAppInfos(filteredApps, appEmbed, cachedBaseData.value)).map {
            info => info.app.id -> info
          }(collection.breakOut)
        } else {
          Map.empty[PathId, AppInfo]
        }

      val statusById: Map[PathId, PodStatus] =
        if (groupEmbedPods) {
          val filteredPods: IndexedSeq[PodDefinition] =
            group.transitivePods.filterAs(selectors.podSelector.matches)(collection.breakOut)
          await(resolvePodInfos(filteredPods, cachedBaseData.value)).map { status =>
            PathId(status.id) -> status
          }(collection.breakOut)
        } else {
          Map.empty[PathId, PodStatus]
        }

      //already matched groups are stored here for performance reasons (match only once)
      val alreadyMatched = mutable.Map.empty[PathId, Boolean]
      def queryGroup(ref: Group): Option[GroupInfo] = {
        //if a subgroup is allowed, we also have to allow all parents implicitly
        def groupMatches(group: Group): Boolean = {
          alreadyMatched.getOrElseUpdate(
            group.id,
            selectors.groupSelector.matches(group) ||
              group.groupsById.exists { case (_, group) => groupMatches(group) } ||
              group.apps.keys.exists(infoById.contains)) || group.pods.keys.exists(statusById.contains)
        }
        if (groupMatches(ref)) {
          val groups: Option[Seq[GroupInfo]] =
            if (groupEmbed(GroupInfo.Embed.Groups))
              Some(ref.groupsById.values.toIndexedSeq.flatMap(queryGroup).sortBy(_.group.id))
            else
              None
          val apps: Option[Seq[AppInfo]] =
            if (groupEmbedApps)
              Some(ref.apps.keys.flatMap(infoById.get)(collection.breakOut).sortBy(_.app.id))
            else
              None
          val pods: Option[Seq[PodStatus]] =
            if (groupEmbedPods)
              Some(ref.pods.keys.flatMap(statusById.get)(collection.breakOut).sortBy(_.id))
            else
              None

          Some(GroupInfo(ref, apps, pods, groups))
        } else None
      }
      queryGroup(group)
    }

  private[this] def resolveAppInfos(
    specs: Seq[RunSpec],
    embed: Set[AppInfo.Embed],
    baseData: AppInfoBaseData = newBaseData()): Future[Seq[AppInfo]] = Future.sequence(specs.collect {
    case app: AppDefinition =>
      baseData.appInfoFuture(app, embed)
  })

  private[this] def resolvePodInfos(
    specs: Seq[RunSpec],
    baseData: AppInfoBaseData): Future[Seq[PodStatus]] = Future.sequence(specs.collect {
    case pod: PodDefinition =>
      baseData.podStatus(pod)
  })
}
